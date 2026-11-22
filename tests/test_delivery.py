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


class DeliveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.upstream = self.root / "upstream.json"
        self.downstream = self.root / "downstream.json"
        self.build = write_json(self.root / "build.json", build_document())
        run_cli("build", "--input", self.build, "--store", self.upstream)
        self.build.unlink()  # the store must stand on its own

    def tearDown(self):
        self.directory.cleanup()

    def export(self, name, *, base=None, full=False):
        path = self.root / name
        args = ["export", "--store", self.upstream, "--output", path]
        args += ["--full"] if full else ["--base-version", base]
        run_cli(*args)
        return path

    def apply(self, package, fault=None, check=True):
        return run_cli(
            "apply", "--store", self.downstream, "--package", package,
            fault=fault, check=check,
        )

    def correct(self, name, document, fault=None, check=True):
        request = write_json(self.root / name, document)
        return run_cli(
            "correct", "--store", self.upstream, "--request", request,
            fault=fault, check=check,
        )

    def query(self, version, sequence, track="marker", **kwargs):
        return json.loads(
            run_cli(
                "query", "--store", self.upstream, "--version", version,
                "--sequence", sequence, "--track", track, **kwargs,
            ).stdout
        )

    def view(self):
        return json.loads(run_cli("view", "--store", self.downstream).stdout)

    def point_query(self, sequence, track="marker"):
        return json.loads(
            run_cli("view", "--store", self.downstream, "--sequence", sequence,
                    "--track", track).stdout
        )

    def point(self, sequence, track="marker"):
        return self.point_query(sequence, track)

    # -- initial delivery ---------------------------------------------------

    def test_full_delivery_contains_three_exact_points(self):
        package = self.export("full.json", full=True)
        document = read_json(package)
        self.assertIsNone(document["base_version"])
        self.assertIsNone(document["base_fingerprint"])
        self.assertEqual(
            [(r["op"], r["sequence"], r["track_id"], r["xyz"]) for r in document["records"]],
            [("upsert", 10, "marker", ["2", "1", "4"]),
             ("upsert", 20, "marker", ["2", "1", "4"]),
             ("upsert", 30, "marker", ["2", "1", "4"])],
        )
        self.assertNotIn("observations", json.dumps(document))
        self.apply(package)
        self.assertEqual(self.view()["version"], 0)
        for sequence in (10, 20, 30):
            self.assertEqual(self.point(sequence)["xyz"], ["2", "1", "4"])

    # -- the required negative-depth scenario -------------------------------

    def test_negative_depth_correction_retracts_two_points_then_restores_one(self):
        self.apply(self.export("full.json", full=True))

        summary = json.loads(
            self.correct(
                "c1.json",
                change_document("c1", 0, [replacement(10, 20, last="1")]),
            ).stdout
        )
        self.assertEqual((summary["status"], summary["version"]), ("applied", 1))

        affected = self.query(1, 10)
        self.assertEqual(affected["status"], "reconstructed")
        self.assertEqual(affected["xyz"], ["-2", "-1", "-4"])
        self.assertFalse(affected["deliverable"])
        self.assertEqual(affected["reason"], "nonpositive_depth")
        self.assertEqual(affected["reasons"], ["nonpositive_depth"])
        self.assertEqual(
            [observation["source"]["source"] for observation in affected["observations"]],
            ["initial", "change"],
        )
        self.assertEqual(
            affected["calibration_sources"]["right"]["change_id"], "c1"
        )
        self.assertEqual({o["squared_residual"] for o in affected["observations"]}, {"0"})
        self.assertEqual(self.query(1, 20)["deliverable"], False)
        self.assertEqual(self.query(1, 30)["deliverable"], True)

        # The earlier version is untouched by the correction.
        old = self.query(0, 10)
        self.assertEqual(old["xyz"], ["2", "1", "4"])
        self.assertTrue(old["deliverable"])
        self.assertEqual(old["calibration_sources"]["right"]["source"], "initial")

        retraction = self.export("d1.json", base=0)
        records = read_json(retraction)["records"]
        self.assertEqual(
            records,
            [{"op": "delete", "sequence": 10, "track_id": "marker"},
             {"op": "delete", "sequence": 20, "track_id": "marker"}],
        )
        self.apply(retraction)
        self.assertEqual(self.view()["version"], 1)
        self.assertFalse(self.point(10)["found"])
        self.assertFalse(self.point(20)["found"])
        self.assertEqual(self.point(30)["xyz"], ["2", "1", "4"])

        self.correct("c2.json", change_document("c2", 1, [replacement(20, 30, last="-1")]))
        restore = self.export("d2.json", base=1)
        self.assertEqual(
            read_json(restore)["records"],
            [{"op": "upsert", "sequence": 20, "track_id": "marker",
              "xyz": ["2", "1", "4"]}],
        )
        self.apply(restore)
        self.assertFalse(self.point(10)["found"])
        self.assertEqual(self.point(20)["xyz"], ["2", "1", "4"])
        self.assertEqual(self.point(30)["xyz"], ["2", "1", "4"])
        self.assertEqual(self.view()["version"], 2)

    # -- other delivery semantics ------------------------------------------

    def test_xyz_update_and_empty_delta_both_advance_versions(self):
        self.apply(self.export("full.json", full=True))
        self.correct(
            "unused.json",
            change_document("unused", 0, [replacement(500, 600, last="-3")]),
        )
        empty = self.export("empty.json", base=0)
        self.assertEqual(read_json(empty)["records"], [])
        summary = json.loads(self.apply(empty).stdout)
        self.assertEqual((summary["status"], summary["version"]), ("applied", 1))
        self.assertEqual(self.view()["version"], 1)
        self.assertEqual(self.point(10)["xyz"], ["2", "1", "4"])

        self.correct(
            "translation.json",
            change_document(
                "translation", 1,
                [{"camera_id": "right", "start": 0, "end": 10**9,
                  "projection": right_projection("-2")}],
            ),
        )
        updated = self.query(2, 10)
        self.assertTrue(updated["deliverable"])
        self.assertEqual(updated["xyz"], ["4", "2", "8"])
        delta = read_json(self.export("xyz.json", base=1))
        self.assertEqual(
            [(r["op"], r["xyz"]) for r in delta["records"]],
            [("upsert", ["4", "2", "8"])] * 3,
        )
        self.apply(self.export("xyz.json", base=1))
        self.assertEqual(self.point(10)["xyz"], ["4", "2", "8"])
        self.assertEqual(self.view()["version"], 2)

    def test_historical_export_bytes_are_stable(self):
        self.apply(self.export("full.json", full=True))
        self.correct("c1.json", change_document("c1", 0, [replacement(10, 20, last="1")]))
        first = self.export("d1.json", base=0).read_bytes()
        self.correct("c2.json", change_document("c2", 1, [replacement(20, 30, last="-1")]))
        again = self.export("d1-again.json", base=0).read_bytes()
        self.assertEqual(first, again)

    # -- idempotency and validation ----------------------------------------

    def test_change_retry_and_conflicts(self):
        self.correct("c1.json", change_document("c1", 0, [replacement(10, 20, last="1")]))
        retry = self.correct(
            "c1-retry.json", change_document("c1", 0, [replacement(10, 20, last="1")])
        )
        summary = json.loads(retry.stdout)
        self.assertEqual((summary["status"], summary["version"]), ("already_applied", 1))
        self.assertEqual(read_json(self.upstream)["version"], 1)

        # Same id, different values is rejected even after later versions.
        conflict = self.correct(
            "c1-conflict.json",
            change_document("c1", 0, [replacement(10, 21, last="1")]),
            check=False,
        )
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("already used", conflict.stderr)

        stale = self.correct(
            "stale.json", change_document("stale", 0, [replacement(10, 20, last="1")]),
            check=False,
        )
        self.assertEqual(stale.returncode, 2)
        self.assertIn("stale", stale.stderr)

        unknown = self.correct(
            "unknown.json", change_document("unknown", 1, [replacement(1, 2, camera="ghost")]),
            check=False,
        )
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown camera", unknown.stderr)

        overlap = self.correct(
            "overlap.json",
            change_document("overlap", 1, [replacement(10, 20), replacement(20, 30)]),
            check=False,
        )
        self.assertEqual(overlap.returncode, 2)
        self.assertIn("overlapping", overlap.stderr)

        bad_rank = self.correct(
            "rank.json",
            change_document(
                "rank", 1,
                [{"camera_id": "right", "start": 0, "end": 5,
                  "projection": [["0", "0", "0", "0"]] * 3}],
            ),
            check=False,
        )
        self.assertEqual(bad_rank.returncode, 2)

        # None of the failures consumed an id or a version.
        state = read_json(self.upstream)
        self.assertEqual(state["version"], 1)
        self.assertEqual(list(state["changes"]), ["c1"])

        reclaimed = self.correct(
            "reclaimed.json", change_document("stale", 1, [replacement(10, 20, last="1")])
        )
        self.assertEqual(json.loads(reclaimed.stdout)["version"], 2)

    def test_change_equality_ignores_key_and_replacement_order(self):
        first = {
            "dataset_id": "site-a", "change_id": "same", "base_version": 0,
            "replacements": [
                replacement(10, 20, last="1"),
                replacement(100, 200, last="-2"),
            ],
        }
        second = {
            "replacements": [
                {"end": 200, "projection": right_projection("-2"),
                 "start": 100, "camera_id": "right"},
                {"projection": right_projection("1"), "end": 20,
                 "camera_id": "right", "start": 10},
            ],
            "base_version": 0, "change_id": "same", "dataset_id": "site-a",
        }
        self.correct("a.json", first)
        summary = json.loads(self.correct("b.json", second).stdout)
        self.assertEqual((summary["status"], summary["version"]), ("already_applied", 1))

        # Matrix text participates in equality even when numerically equal.
        different_text = {
            "dataset_id": "site-a", "change_id": "same", "base_version": 0,
            "replacements": [
                replacement(10, 20, last="1"),
                replacement(100, 200, last="-2.0"),
            ],
        }
        rejected = self.correct("c.json", different_text, check=False)
        self.assertEqual(rejected.returncode, 2)

    def test_cross_request_overlap_newest_wins_per_sequence(self):
        self.correct("a.json", change_document("a", 0, [replacement(5, 25, last="1")]))
        self.correct("b.json", change_document("b", 1, [replacement(15, 30, last="-1")]))
        # 10 is covered only by a; 20 and 30 by b.
        record = self.query(2, 10)
        self.assertFalse(record["deliverable"])
        self.assertEqual(
            (record["calibration_sources"]["right"]["change_id"],
             record["calibration_sources"]["right"]["version"]),
            ("a", 1),
        )
        for sequence in (20, 30):
            record = self.query(2, sequence)
            self.assertTrue(record["deliverable"])
            self.assertEqual(
                record["calibration_sources"]["right"]["change_id"], "b"
            )
        self.assertEqual(
            read_json(self.export("d1.json", base=0))["records"],
            [{"op": "delete", "sequence": 10, "track_id": "marker"},
             {"op": "delete", "sequence": 20, "track_id": "marker"}],
        )
        self.assertEqual(
            read_json(self.export("d2.json", base=1))["records"],
            [{"op": "upsert", "sequence": 20, "track_id": "marker",
              "xyz": ["2", "1", "4"]}],
        )

    def test_per_camera_intervals_are_independent(self):
        document = change_document(
            "both", 0,
            [
                replacement(0, 100, camera="left"),
                replacement(0, 20, last="1"),
            ],
        )
        self.correct("both.json", document)
        record = self.query(1, 20)
        self.assertEqual(
            {camera: source["change_id"] for camera, source in record["calibration_sources"].items()},
            {"left": "both", "right": "both"},
        )
        # Sequence 30 lies outside both right-camera intervals, so the right
        # camera keeps its initial calibration while left still uses the change.
        outside = self.query(1, 30)["calibration_sources"]
        self.assertEqual(
            (outside["left"]["change_id"], outside["right"]["source"]), ("both", "initial")
        )

    def test_touching_intervals_are_disjoint(self):
        self.correct(
            "touching.json",
            change_document(
                "touching", 0,
                [replacement(0, 10), replacement(11, 20)],
            ),
        )
        self.assertEqual(self.query(1, 10)["calibration_sources"]["right"]["change_id"], "touching")
        self.assertEqual(self.query(1, 20)["calibration_sources"]["right"]["change_id"], "touching")


if __name__ == "__main__":
    unittest.main()
