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


class DownstreamContractTests(unittest.TestCase):
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
        self.full = self.export("full.json", full=True)
        run_cli("apply", "--store", self.downstream, "--package", self.full)
        self.correct(
            "c1.json", change_document("c1", 0, [replacement(10, 20, last="1")])
        )
        self.retraction = self.export("retract.json", base=0)

    def tearDown(self):
        self.directory.cleanup()

    def export(self, name, base=None, full=False):
        path = self.root / name
        arguments = ["export", "--store", self.upstream, "--output", path]
        arguments += ["--full"] if full else ["--base-version", base]
        run_cli(*arguments)
        return path

    def correct(self, name, document):
        run_cli(
            "correct", "--store", self.upstream, "--request",
            write_json(self.root / name, document),
        )

    def apply(self, package, check=False):
        return run_cli("apply", "--store", self.downstream, "--package", package,
                       check=check)

    def view(self):
        return json.loads(run_cli("view", "--store", self.downstream).stdout)

    def reject(self, package, needle):
        before = self.view()
        result = self.apply(package)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(needle, result.stderr)
        self.assertEqual(self.view(), before)
        return result

    def test_delta_before_full_is_rejected_by_empty_store(self):
        empty = self.root / "empty.json"
        self.assertEqual(self.view_of(empty)["version"], None)
        result = run_cli("apply", "--store", empty, "--package", self.retraction,
                         check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("only the first full delivery", result.stderr)
        self.assertFalse(empty.exists())
        self.assertEqual(self.view_of(empty)["version"], None)

    def view_of(self, path):
        return json.loads(run_cli("view", "--store", path).stdout)

    def test_skipped_version_does_not_change_downstream(self):
        # Upstream moves to version 2 while downstream is still at 0, so the
        # 1->2 package is an exact next version for the exporter but a gap
        # for this store.
        self.correct(
            "c2.json", change_document("c2", 1, [replacement(20, 30, last="-1")])
        )
        later = self.export("later.json", base=1)
        self.reject(later, "skips versions")

    def test_next_version_with_wrong_base_fingerprint_is_rejected(self):
        payload = json.loads(self.retraction.read_text())
        payload["base_fingerprint"] = "0" * 64
        self.reject(write_json(self.root / "wrongbase.json", payload),
                    "base_fingerprint does not match")

    def test_corrupt_package_is_rejected(self):
        payload = json.loads(self.retraction.read_text())
        payload["records"][0]["xyz"] = ["2", "1", "4"]
        corrupt = write_json(self.root / "corrupt.json", payload)
        self.reject(corrupt, "unexpected field")

        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.reject(broken, "Expecting")

        truncated = self.root / "truncated.json"
        truncated.write_bytes(self.retraction.read_bytes()[:40])
        self.reject(truncated, "line 1")

        wrong_kind = json.loads(self.retraction.read_text())
        wrong_kind["kind"] = "something-else"
        self.reject(write_json(self.root / "kind.json", wrong_kind),
                    "not a camera-tracks delivery file")

        boolean_sequence = json.loads(self.retraction.read_text())
        boolean_sequence["records"][0]["sequence"] = True
        self.reject(write_json(self.root / "bool.json", boolean_sequence),
                    "must be an integer")

    def test_fingerprint_mismatch_is_rejected(self):
        payload = json.loads(self.retraction.read_text())
        payload["target_fingerprint"] = "0" * 64
        self.reject(write_json(self.root / "fp.json", payload),
                    "does not match target_fingerprint")

        payload = json.loads(self.retraction.read_text())
        payload["base_fingerprint"] = "0" * 64
        self.reject(write_json(self.root / "basefp.json", payload),
                    "base_fingerprint does not match")

    def test_delete_of_missing_point_is_rejected(self):
        base = json.loads(run_cli("view", "--store", self.downstream).stdout)
        payload = json.loads(self.retraction.read_text())
        payload["base_fingerprint"] = base["fingerprint"]
        payload["records"] = [{"op": "delete", "sequence": 100, "track_id": "marker"}]
        self.reject(write_json(self.root / "extra-delete.json", payload),
                    "delete of unknown point")

    def test_wrong_dataset_is_rejected(self):
        other_upstream = self.root / "other-upstream.json"
        run_cli(
            "build", "--input",
            write_json(self.root / "other-build.json", build_document(dataset_id="site-b")),
            "--store", other_upstream,
        )
        package = self.root / "other-full.json"
        run_cli("export", "--store", other_upstream, "--output", package, "--full")
        self.reject(package, "does not match downstream dataset")

    def test_alternate_bytes_for_same_target_cannot_overwrite(self):
        # Apply the real retraction first, then try an equivalent but
        # differently encoded package claiming the same target version.
        applied = json.loads(self.apply(self.retraction, check=True).stdout)
        self.assertEqual(applied["version"], 1)
        equivalent = json.loads(self.retraction.read_text())
        forged = write_json(self.root / "forged.json", equivalent)
        result = run_cli("apply", "--store", self.downstream, "--package", forged,
                         check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not after current version", result.stderr)

    def test_full_delivery_retry_is_idempotent(self):
        result = json.loads(
            run_cli("apply", "--store", self.downstream, "--package", self.full).stdout
        )
        self.assertEqual((result["status"], result["target_version"]), ("already_applied", 0))
        self.assertEqual(self.view()["version"], 0)

    def test_retry_after_multiple_updates_answers_original_version(self):
        self.apply(self.retraction, check=True)
        self.correct(
            "c2.json", change_document("c2", 1, [replacement(20, 30, last="-1")])
        )
        restore = self.export("restore.json", base=1)
        self.apply(restore, check=True)
        retry = json.loads(self.apply(self.retraction, check=True).stdout)
        self.assertEqual(
            (retry["status"], retry["target_version"], retry["version"]),
            ("already_applied", 1, 2),
        )
        view = self.view()
        self.assertEqual(view["version"], 2)
        self.assertFalse(
            json.loads(run_cli("view", "--store", self.downstream,
                               "--sequence", 10, "--track", "marker").stdout)["found"]
        )

    def test_upstream_store_and_downstream_store_are_independent(self):
        # Applying a package must not touch the upstream store's bytes.
        before = self.upstream.read_bytes()
        self.apply(self.retraction, check=True)
        self.assertEqual(self.upstream.read_bytes(), before)
        # And the upstream export remains reproducible afterwards.
        again = self.export("retract-again.json", base=0)
        self.assertEqual(again.read_bytes(), self.retraction.read_bytes())

    def test_export_refuses_to_overwrite_its_own_store(self):
        result = run_cli("export", "--store", self.upstream, "--output",
                         self.upstream, "--full", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must differ", result.stderr)
        self.assertEqual(read_json(self.upstream)["version"], 1)

    def test_export_current_view_and_version(self):
        self.apply(self.retraction, check=True)
        output = self.root / "view.json"
        summary = json.loads(
            run_cli("view", "--store", self.downstream, "--output", output).stdout
        )
        document = read_json(output)
        self.assertEqual(summary["version"], 1)
        self.assertEqual(document["fingerprint"], summary["fingerprint"])
        self.assertEqual(
            [(p["sequence"], p["track_id"], p["xyz"]) for p in document["points"]],
            [(30, "marker", ["2", "1", "4"])],
        )


class BusinessOutcomeTests(unittest.TestCase):
    """Unobservable, zero-depth and over-threshold points stay business results."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def build(self, document, name="state.json"):
        path = write_json(self.root / f"{name}.input.json", document)
        store = self.root / name
        run_cli("build", "--input", path, "--store", store)
        return store

    def query(self, store, version, sequence=10):
        return json.loads(
            run_cli("query", "--store", store, "--version", version,
                    "--sequence", sequence, "--track", "marker").stdout
        )

    def test_unobservable_is_a_point_result_and_other_batches_remain(self):
        document = build_document(sequences=(10, 20))
        mirror = {
            "id": "mirror",
            "projection": [["1", "0", "0", "0"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
        }
        document["cameras"].append(mirror)
        # Batch 10 keeps left+right; batch 20 observes two identical views.
        document["batches"][1]["tracks"][0]["observations"] = [
            {"camera_id": "left", "xy": ["0.5", "0.25"], "weight": "1"},
            {"camera_id": "mirror", "xy": ["0.5", "0.25"], "weight": "1"},
        ]
        store = self.build(document)
        record = self.query(store, 0, 20)
        self.assertEqual(record["status"], "unobservable")
        self.assertIsNone(record["xyz"])
        self.assertEqual(record["reason"], "unobservable")
        self.assertFalse(record["deliverable"])
        self.assertTrue(self.query(store, 0, 10)["deliverable"])

    def test_zero_depth_reports_nonpositive(self):
        document = build_document(sequences=(10,))
        # A third camera whose optical plane is Z=4 sees the sample point
        # exactly at its own zero-depth plane.
        document["cameras"].append(
            {
                "id": "probe",
                "projection": [["1", "0", "0", "-2"], ["0", "1", "0", "-1"],
                               ["0", "0", "1", "-4"]],
            }
        )
        document["batches"][0]["tracks"][0]["observations"].append(
            {"camera_id": "probe", "xy": ["0.5", "0.25"], "weight": "1"}
        )
        store = self.build(document)
        record = self.query(store, 0)
        self.assertEqual(record["xyz"], ["2", "1", "4"])
        depths = {o["camera_id"]: o["depth"] for o in record["observations"]}
        self.assertEqual(depths, {"left": "4", "right": "4", "probe": "0"})
        self.assertEqual(record["reasons"], ["nonpositive_depth"])
        self.assertFalse(record["deliverable"])

    def test_over_threshold_uses_squared_residual(self):
        document = build_document(sequences=(10,), threshold="0.01")
        document["batches"][0]["tracks"][0]["observations"][1]["xy"] = ["0.25", "0.3"]
        store = self.build(document)
        record = self.query(store, 0)
        self.assertEqual(record["status"], "reconstructed")
        self.assertFalse(record["deliverable"])
        self.assertEqual(record["reason"], "excessive_reprojection_error")
        self.assertEqual(record["reasons"], ["excessive_reprojection_error"])
        squares = [o["squared_residual"] for o in record["observations"]]
        self.assertTrue(all(s != "0" for s in squares))
        self.assertEqual(squares, ["13/20000", "13/20000"])

        # The same data with a generous threshold becomes deliverable.
        generous = build_document(sequences=(10,), threshold="0.1")
        generous["batches"][0]["tracks"][0]["observations"][1]["xy"] = ["0.25", "0.3"]
        record = self.query(self.build(generous, "generous.json"), 0)
        self.assertTrue(record["deliverable"])

    def test_nonpositive_depth_precedes_error(self):
        document = build_document(sequences=(10,))
        document["cameras"][1]["projection"] = [
            ["1", "0", "0", "1"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]
        ]
        document["batches"][0]["tracks"][0]["observations"][1]["xy"] = ["0.25", "0.3"]
        store = self.build(document)
        record = self.query(store, 0)
        self.assertEqual(
            record["reasons"], ["nonpositive_depth", "excessive_reprojection_error"]
        )
        self.assertEqual(record["reason"], "nonpositive_depth")

    def test_unobservable_precedes_depth(self):
        document = build_document(sequences=(10,))
        mirror = {
            "id": "mirror",
            "projection": [["1", "0", "0", "0"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
        }
        document["cameras"].append(mirror)
        document["batches"][0]["tracks"][0]["observations"] = [
            {"camera_id": "left", "xy": ["-0.5", "-0.25"], "weight": "1"},
            {"camera_id": "mirror", "xy": ["-0.5", "-0.25"], "weight": "1"},
        ]
        store = self.build(document)
        record = self.query(store, 0)
        self.assertEqual((record["status"], record["reason"]), ("unobservable", "unobservable"))

    def test_negative_depth_point_retains_report(self):
        document = build_document(sequences=(10,))
        for observation in document["batches"][0]["tracks"][0]["observations"]:
            observation["xy"] = ["-0.5", "-0.25"] if observation["camera_id"] == "left" else ["-0.25", "-0.25"]
        store = self.build(document)
        record = self.query(store, 0)
        self.assertEqual(record["xyz"], ["2", "1", "-4"])
        self.assertEqual(record["reason"], "nonpositive_depth")
        self.assertEqual(record["reasons"], ["nonpositive_depth"])
        self.assertEqual(
            [o["depth"] for o in record["observations"]], ["-4", "-4"]
        )


if __name__ == "__main__":
    unittest.main()
