import json
from pathlib import Path
import tempfile
import unittest

from helpers import (
    build_document,
    change_document,
    read_json,
    replacement,
    right_projection,
    run_cli,
    write_json,
)


class InterruptionTests(unittest.TestCase):
    """Downstream atomicity and upstream transaction visibility."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.upstream = self.root / "upstream.json"
        self.downstream = self.root / "downstream.json"
        run_cli(
            "build",
            "--input", write_json(self.root / "build.json", build_document()),
            "--store", self.upstream,
        )
        run_cli(
            "export", "--store", self.upstream, "--output", self.root / "full.json",
            "--full",
        )
        run_cli(
            "apply", "--store", self.downstream, "--package", self.root / "full.json"
        )

    def tearDown(self):
        self.directory.cleanup()

    def correct(self, name, document, fault=None):
        request = write_json(self.root / name, document)
        return run_cli(
            "correct", "--store", self.upstream, "--request", request,
            fault=fault, check=False,
        )

    def export(self, name, base):
        path = self.root / name
        run_cli("export", "--store", self.upstream, "--output", path,
                "--base-version", base)
        return path

    def apply(self, package, fault=None):
        return run_cli(
            "apply", "--store", self.downstream, "--package", package,
            fault=fault, check=False,
        )

    def view(self):
        return json.loads(run_cli("view", "--store", self.downstream).stdout)

    def point(self, sequence):
        return json.loads(
            run_cli("view", "--store", self.downstream, "--sequence", sequence,
                    "--track", "marker").stdout
        )

    def prepare_retraction(self):
        self.correct(
            "c1.json", change_document("c1", 0, [replacement(10, 20, last="1")])
        )
        return self.export("retract.json", 0)

    # -- interruption before the downstream commit -------------------------

    def test_interrupt_before_apply_commit_keeps_old_view(self):
        retraction = self.prepare_retraction()
        before = self.view()
        old_bytes = self.downstream.read_bytes()

        failed = self.apply(retraction, fault="apply-before-commit")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected interruption", failed.stderr)
        self.assertEqual(self.downstream.read_bytes(), old_bytes)

        after = self.view()
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["fingerprint"], before["fingerprint"])
        for sequence in (10, 20, 30):
            self.assertEqual(self.point(sequence)["xyz"], ["2", "1", "4"])

        applied = json.loads(self.apply(retraction).stdout)
        self.assertEqual((applied["status"], applied["version"]), ("applied", 1))
        self.assertFalse(self.point(10)["found"])
        self.assertFalse(self.point(20)["found"])
        self.assertTrue(self.point(30)["found"])
        self.assertEqual(self.view()["version"], 1)

    # -- interruption after the downstream commit --------------------------

    def test_interrupt_after_apply_commit_then_restore_then_retry(self):
        retraction = self.prepare_retraction()
        failed = self.apply(retraction, fault="apply-after-commit")
        self.assertNotEqual(failed.returncode, 0)

        # The retraction is durable even though the caller saw a failure.
        self.assertEqual(self.view()["version"], 1)
        self.assertFalse(self.point(10)["found"])

        self.correct(
            "c2.json", change_document("c2", 1, [replacement(20, 30, last="-1")])
        )
        restore = self.export("restore.json", 1)
        applied = json.loads(self.apply(restore).stdout)
        self.assertEqual((applied["status"], applied["version"]), ("applied", 2))
        restored = self.view()
        self.assertFalse(self.point(10)["found"])
        self.assertEqual(self.point(20)["xyz"], ["2", "1", "4"])
        self.assertEqual(self.point(30)["xyz"], ["2", "1", "4"])

        retry = json.loads(self.apply(retraction).stdout)
        self.assertEqual(
            (retry["status"], retry["target_version"]), ("already_applied", 1)
        )
        self.assertEqual(retry["version"], 2)
        final = self.view()
        self.assertEqual(final["version"], 2)
        self.assertEqual(final["fingerprint"], restored["fingerprint"])
        self.assertFalse(self.point(10)["found"])
        self.assertEqual(self.point(20)["xyz"], ["2", "1", "4"])

    # -- upstream correction transaction -----------------------------------

    def test_interrupt_before_correct_commit_keeps_version_and_export(self):
        state_before = self.upstream.read_bytes()
        failed = self.correct(
            "c1.json",
            change_document("c1", 0, [replacement(10, 20, last="1")]),
            fault="correct-before-commit",
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.upstream.read_bytes(), state_before)

        state = read_json(self.upstream)
        self.assertEqual(state["version"], 0)
        self.assertEqual(state["changes"], {})

        rejected = run_cli(
            "export", "--store", self.upstream, "--output", self.root / "none.json",
            "--base-version", 0,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("cannot export versions 0->1", rejected.stderr)
        self.assertFalse((self.root / "none.json").exists())

        query = run_cli(
            "query", "--store", self.upstream, "--version", 1,
            "--sequence", 10, "--track", "marker", check=False,
        )
        self.assertEqual(query.returncode, 2)
        self.assertIn("has not been created", query.stderr)

        retry = self.correct(
            "c1.json", change_document("c1", 0, [replacement(10, 20, last="1")])
        )
        summary = json.loads(retry.stdout)
        self.assertEqual((summary["status"], summary["version"]), ("applied", 1))

    def test_interrupt_after_correct_commit_leaves_consistent_version(self):
        failed = self.correct(
            "c1.json",
            change_document("c1", 0, [replacement(10, 20, last="1")]),
            fault="correct-after-commit",
        )
        self.assertNotEqual(failed.returncode, 0)

        state = read_json(self.upstream)
        self.assertEqual(state["version"], 1)
        self.assertIn("c1", state["changes"])

        # Version, change id, query and export visibility all already coexist.
        query = json.loads(
            run_cli("query", "--store", self.upstream, "--version", 1,
                    "--sequence", 10, "--track", "marker").stdout
        )
        self.assertFalse(query["deliverable"])
        package = self.export("retract.json", 0)
        self.assertEqual(
            [r["op"] for r in read_json(package)["records"]], ["delete", "delete"]
        )

        # The retry is answered idempotently, without another version.
        retry = json.loads(
            self.correct(
                "c1.json", change_document("c1", 0, [replacement(10, 20, last="1")])
            ).stdout
        )
        self.assertEqual((retry["status"], retry["version"]), ("already_applied", 1))
        self.assertEqual(read_json(self.upstream)["version"], 1)

    def test_interrupt_before_export_write_leaves_no_half_file(self):
        self.correct(
            "c1.json", change_document("c1", 0, [replacement(10, 20, last="1")])
        )
        output = self.root / "partial.json"
        failed = run_cli(
            "export", "--store", self.upstream, "--output", output,
            "--base-version", 0, fault="export-before-write", check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(output.exists())
        self.assertEqual([p.name for p in self.root.glob(".publish-*")], [])
        # Export works normally afterwards and is still the full retraction.
        retraction = self.export("retract.json", 0)
        self.assertEqual(len(read_json(retraction)["records"]), 2)


if __name__ == "__main__":
    unittest.main()
