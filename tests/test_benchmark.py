"""Training-material and blind-benchmark release registry tests.

Covers the acceptance walkthrough (same-batch groups, exactly-at-limit
passing, release without download, late ties withdrawing only the affected
booklet), idempotent replay and request normalisation, exposure by batch and
relation, byte-stable re-export of issued material, first-withdrawal evidence
immutability, real interruption boundaries, erroneous fingerprints and
versions, whole-batch relation rejection, and the 4x100x200 capacity batch.
Where point tables are needed they come from the real build/export/apply/view
delivery chain.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from helpers import SRC, read_json, run_cli, write_json

sys.path.insert(0, str(SRC))
from camera_tracks import benchmark  # noqa: E402
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402

CONFIG_KIND = "camera-tracks-benchmark-config"
TRAINING_KIND = "camera-tracks-training-package"
BOOKLET_KIND = "camera-tracks-benchmark-booklet"
VIEW_KIND = "camera-tracks-view"

LEFT = {
    "id": "left",
    "projection": [["1", "0", "0", "0"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
}
RIGHT = {
    "id": "right",
    "projection": [["1", "0", "0", "-1"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
}


def view_document(points, dataset_id="ds", version=0):
    table = {point_key(s, t): list(xyz) for (s, t), xyz in points.items()}
    return {
        "schema_version": 1,
        "kind": VIEW_KIND,
        "dataset_id": dataset_id,
        "version": version,
        "fingerprint": fingerprint_points(table),
        "points": [
            {"sequence": s, "track_id": t, "xyz": list(xyz)}
            for (s, t), xyz in sorted(points.items())
        ],
    }


def ref(dataset_id, sequence, track_id):
    return {"dataset_id": dataset_id, "sequence": sequence, "track_id": track_id}


def release_request(op_id, release_id, points):
    return {"op_id": op_id, "release_id": release_id, "points": points}


def issue_request(op_id, booklet_id, limit, points):
    return {
        "op_id": op_id,
        "booklet_id": booklet_id,
        "elevation_limit": limit,
        "points": points,
    }


def relate_request(op_id, pairs):
    return {
        "op_id": op_id,
        "relations": [{"points": [first, second]} for first, second in pairs],
    }


def score_request(op_id, booklet_id, sha, prediction_id, predictions):
    return {
        "op_id": op_id,
        "booklet_id": booklet_id,
        "booklet_sha256": sha,
        "prediction_id": prediction_id,
        "predictions": predictions,
    }


class BenchmarkHarness(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="bench-"))
        self.project = self.root / "project.json"
        self.counter = 0

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- project construction ---------------------------------------------

    def make_project(self, datasets, relations=(), name="config.json"):
        """datasets maps dataset_id -> {(sequence, track): [x, y, z]}."""
        view_paths = {}
        entries = []
        for dataset_id, points in datasets.items():
            document = view_document(points, dataset_id=dataset_id)
            path = self.root / f"view-{dataset_id}.json"
            write_json(path, document)
            view_paths[dataset_id] = path
            entries.append({
                "dataset_id": dataset_id,
                "version": 0,
                "fingerprint": document["fingerprint"],
            })
        config = {
            "schema_version": 1,
            "kind": CONFIG_KIND,
            "project_id": "bench",
            "datasets": entries,
            "relations": [
                {"points": [first, second]} for first, second in relations
            ],
        }
        config_path = write_json(self.root / name, config)
        args = ["bench", "--config", config_path, "--project", self.project]
        for dataset_id, path in sorted(view_paths.items()):
            args += ["--view", f"{dataset_id}={path}"]
        result = run_cli(*args)
        return json.loads(result.stdout)

    def write(self, name, document):
        return write_json(self.root / name, document)

    def invoke(self, command, name, document, fault=None, check=True):
        request = self.write(name, document)
        result = run_cli(
            command, "--project", self.project, "--request", request,
            fault=fault, check=check,
        )
        return result

    def ok(self, command, name, document):
        result = self.invoke(command, name, document)
        return json.loads(result.stdout)

    def reject(self, command, name, document, needle):
        result = self.invoke(command, name, document, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(needle, result.stderr)
        return result

    def state(self):
        return read_json(self.project)

    def export(self, kind, material_id, name):
        output = self.root / name
        command = "bench-export-training" if kind == "training" else "bench-export-booklet"
        summary = json.loads(
            run_cli(command, "--project", self.project, "--id", material_id,
                    "--output", output).stdout
        )
        return output, summary

    def scores(self, booklet=None):
        args = ["bench-scores", "--project", self.project]
        if booklet:
            args += ["--booklet", booklet]
        return json.loads(run_cli(*args).stdout)

    def audit(self, booklet=None):
        args = ["bench-audit", "--project", self.project]
        if booklet:
            args += ["--booklet", booklet]
        return json.loads(run_cli(*args).stdout)

    def status(self):
        return json.loads(run_cli("bench-status", "--project", self.project).stdout)


# ---------------------------------------------------------------------------
# The acceptance walkthrough


class AcceptanceWalkthroughTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project(
            {
                "dsA": {
                    (1, "A0"): ["0", "0", "10"],
                    (1, "A1"): ["1", "0", "11"],
                    (2, "H"): ["5", "5", "20"],
                },
                "dsB": {
                    (7, "B0"): ["2", "2", "30"],
                    (7, "B1"): ["3", "2", "31"],
                },
                "dsU": {(9, "U"): ["9", "9", "40"]},
            }
        )
        self.ok("bench-issue", "issue-H.json",
                issue_request("issue-H", "book-H", "1.5", [ref("dsA", 2, "H")]))
        self.ok("bench-issue", "issue-U.json",
                issue_request("issue-U", "book-U", "2", [ref("dsU", 9, "U")]))
        self.book_h, _ = self.export("booklet", "book-H", "book-H.json")
        self.sha_h = hashlib.sha256(self.book_h.read_bytes()).hexdigest()
        self.sha_u = self.state()["booklets"]["book-U"]["sha256"]

    def score_h(self, op_id, prediction_id, z, name=None):
        return self.ok(
            "bench-score", name or f"{op_id}.json",
            score_request(op_id, "book-H", self.sha_h, prediction_id,
                          [{"dataset_id": "dsA", "sequence": 2, "track_id": "H", "z": z}]),
        )

    def score_u(self, op_id, prediction_id, z):
        return self.ok(
            "bench-score", f"{op_id}.json",
            score_request(op_id, "book-U", self.sha_u, prediction_id,
                          [{"dataset_id": "dsU", "sequence": 9, "track_id": "U", "z": z}]),
        )

    def test_exactly_at_limit_passes_and_release_blocks_sibling_batch(self):
        h = self.score_h("score-H-1", "pred-H-1", "21.5")
        self.assertEqual((h["passed"], h["total"]), (1, 1))
        u = self.score_u("score-U-1", "pred-U-1", "40")
        self.assertEqual((u["passed"], u["total"]), (1, 1))

        released = self.ok("bench-release", "release-A0.json",
                           release_request("release-A0", "train-A0", [ref("dsA", 1, "A0")]))
        self.assertEqual(released["withdrawn"], [])
        # Both scores are still comparable without any download.
        self.assertEqual({row["booklet_id"] for row in self.scores()["scores"]},
                         {"book-H", "book-U"})
        # A1 shares A0's batch, so an A1-only booklet must be refused.
        self.reject("bench-issue", "issue-A1.json",
                  issue_request("issue-A1", "book-A1", "1", [ref("dsA", 1, "A1")]),
                  "exposed point")
        self.assertNotIn("book-A1", self.state()["booklets"])

    def test_late_ties_withdraw_only_h(self):
        self.score_h("score-H-1", "pred-H-1", "21.5")
        self.score_u("score-U-1", "pred-U-1", "40")
        self.ok("bench-release", "release-A0.json",
                release_request("release-A0", "train-A0", [ref("dsA", 1, "A0")]))
        # Drop the source views: every subsequent step must rely on the store.
        for path in self.root.glob("view-ds*.json"):
            path.unlink()

        first = self.ok("bench-relate", "relate-AB.json",
                        relate_request("relate-AB", [(ref("dsB", 7, "B0"), ref("dsA", 1, "A1"))]))
        self.assertEqual(first["withdrawn"], [])
        # H still scores; A1-B0 alone does not expose it.
        self.assertEqual(self.score_h("score-H-2", "pred-H-2", "20")["passed"], 1)

        second = self.ok("bench-relate", "relate-BH.json",
                         relate_request("relate-BH", [(ref("dsA", 2, "H"), ref("dsB", 7, "B1"))]))
        self.assertEqual(second["withdrawn"], ["book-H"])

        # H leaves the comparable table entirely; U's score is untouched.
        comparable = self.scores()
        self.assertEqual([row["booklet_id"] for row in comparable["scores"]], ["book-U"])
        self.assertEqual(comparable["scores"][0]["passed"], 1)
        self.assertEqual(self.scores("book-H")["scores"], [])
        self.assertFalse(self.scores("book-H")["comparable"])

        # H refuses new predictions but keeps its historical scores.
        self.reject("bench-score", "score-H-3.json",
                  score_request("score-H-3", "book-H", self.sha_h, "pred-H-3",
                                [{"dataset_id": "dsA", "sequence": 2, "track_id": "H", "z": "20"}]),
                  "withdrawn")

        audit = self.audit("book-H")["booklet"]
        self.assertTrue(audit["withdrawn"])
        self.assertEqual([p["prediction_id"] for p in audit["predictions"]],
                         ["pred-H-1", "pred-H-2"])
        self.assertEqual([p["passed"] for p in audit["predictions"]], [1, 1])
        evidence = audit["withdrawal"]
        self.assertEqual(evidence["op_id"], "relate-BH")
        self.assertEqual(evidence["release_id"], "train-A0")
        self.assertEqual(evidence["test_point"], ref("dsA", 2, "H"))
        self.assertEqual(evidence["training_point"], ref("dsA", 1, "A0"))

        # U keeps scoring.
        self.assertEqual(self.score_u("score-U-2", "pred-U-2", "41")["passed"], 1)

    def test_withdrawn_booklet_reexports_identical_without_true_z(self):
        self.score_h("score-H-1", "pred-H-1", "21.5")
        self.ok("bench-release", "release-A0.json",
                release_request("release-A0", "train-A0", [ref("dsA", 1, "A0")]))
        self.ok("bench-relate", "relate-AB.json",
                relate_request("relate-AB", [(ref("dsB", 7, "B0"), ref("dsA", 1, "A1"))]))
        self.ok("bench-relate", "relate-BH.json",
                relate_request("relate-BH", [(ref("dsA", 2, "H"), ref("dsB", 7, "B1"))]))
        again, summary = self.export("booklet", "book-H", "book-H-again.json")
        self.assertEqual(again.read_bytes(), self.book_h.read_bytes())
        self.assertEqual(summary["sha256"], self.sha_h)
        document = read_json(again)
        self.assertEqual(document["kind"], BOOKLET_KIND)
        self.assertNotIn('"z"', again.read_text(encoding="utf-8"))
        self.assertEqual(document["points"][0]["xy"], ["5", "5"])

    def test_replay_old_operations_keeps_first_withdrawal_lists(self):
        self.score_h("score-H-1", "pred-H-1", "21.5")
        self.score_u("score-U-1", "pred-U-1", "40")
        release = self.ok("bench-release", "release-A0.json",
                          release_request("release-A0", "train-A0", [ref("dsA", 1, "A0")]))
        self.ok("bench-relate", "relate-AB.json",
                relate_request("relate-AB", [(ref("dsB", 7, "B0"), ref("dsA", 1, "A1"))]))
        tie_h = self.ok("bench-relate", "relate-BH.json",
                        relate_request("relate-BH", [(ref("dsA", 2, "H"), ref("dsB", 7, "B1"))]))
        self.assertEqual(tie_h["withdrawn"], ["book-H"])
        # A second release (not a relation) withdraws U.
        release_u = self.ok("bench-release", "release-U.json",
                            release_request("release-U", "train-U", [ref("dsU", 9, "U")]))
        self.assertEqual(release_u["withdrawn"], ["book-U"])

        # Replaying either old operation returns its original receipt.
        replay_release = self.ok("bench-release", "release-A0.json",
                                 release_request("release-A0", "train-A0", [ref("dsA", 1, "A0")]))
        self.assertEqual(replay_release, release)
        replay_tie = self.ok("bench-relate", "relate-BH.json",
                             relate_request("relate-BH", [(ref("dsA", 2, "H"), ref("dsB", 7, "B1"))]))
        self.assertEqual(replay_tie, tie_h)
        replay_u = self.ok("bench-release", "release-U.json",
                           release_request("release-U", "train-U", [ref("dsU", 9, "U")]))
        self.assertEqual(replay_u, release_u)
        # The two withdrawal lists never cross.
        self.assertEqual(self.audit("book-H")["booklet"]["withdrawal"]["op_id"], "relate-BH")
        self.assertEqual(self.audit("book-U")["booklet"]["withdrawal"]["op_id"], "release-U")
        self.assertEqual(replay_tie["withdrawn"], ["book-H"])
        self.assertEqual(replay_u["withdrawn"], ["book-U"])

    def test_withdrawal_evidence_survives_later_relations(self):
        self.ok("bench-release", "release-A0.json",
                release_request("release-A0", "train-A0", [ref("dsA", 1, "A0")]))
        self.ok("bench-relate", "relate-AB.json",
                relate_request("relate-AB", [(ref("dsB", 7, "B0"), ref("dsA", 1, "A1"))]))
        self.ok("bench-relate", "relate-BH.json",
                relate_request("relate-BH", [(ref("dsA", 2, "H"), ref("dsB", 7, "B1"))]))
        before = self.audit("book-H")["booklet"]["withdrawal"]
        # A later relation could connect H to U and to more training points.
        self.ok("bench-relate", "relate-HU.json",
                relate_request("relate-HU", [(ref("dsA", 2, "H"), ref("dsU", 9, "U"))]))
        after = self.audit("book-H")["booklet"]["withdrawal"]
        self.assertEqual(before, after)


class TrainingPackageTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({"ds": {
            (1, "a"): ["1", "2", "3"],
            (1, "b"): ["4", "5", "6"],
            (2, "c"): ["7", "8", "9"],
        }})

    def test_training_package_has_full_xyz_and_source(self):
        self.ok("bench-release", "release.json",
                release_request("release", "train-1",
                                [ref("ds", 1, "a"), ref("ds", 1, "b")]))
        path, summary = self.export("training", "train-1", "train-1.json")
        document = read_json(path)
        self.assertEqual(document["kind"], TRAINING_KIND)
        self.assertEqual(summary["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual([p["track_id"] for p in document["points"]], ["a", "b"])
        self.assertEqual(document["points"][0]["xyz"], ["1", "2", "3"])
        self.assertEqual(document["points"][0]["source"]["dataset_id"], "ds")
        # Only the listed points; the same batch's other point is exposed but
        # never delivered.
        self.assertEqual(document["count"], 2)

    def test_releases_append_and_old_exports_survive(self):
        self.ok("bench-release", "release-1.json",
                release_request("release-1", "train-1", [ref("ds", 1, "a")]))
        first, first_summary = self.export("training", "train-1", "train-1.json")
        original = first.read_bytes()
        self.ok("bench-release", "release-2.json",
                release_request("release-2", "train-2", [ref("ds", 2, "c")]))
        again, again_summary = self.export("training", "train-1", "train-1-again.json")
        self.assertEqual(again.read_bytes(), original)
        self.assertEqual(again_summary["sha256"], first_summary["sha256"])


class IssuanceRulesTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({"ds": {
            (1, "a"): ["0", "0", "1"],
            (1, "b"): ["0", "0", "2"],
            (2, "c"): ["0", "0", "3"],
        }})

    def test_same_batch_point_makes_booklet_unsafe(self):
        self.ok("bench-release", "release.json",
                release_request("release", "train-1", [ref("ds", 1, "a")]))
        self.reject("bench-issue", "issue.json",
                  issue_request("issue", "book", "0", [ref("ds", 1, "b")]),
                  "exposed point")
        # A point in another batch is still safe.
        self.ok("bench-issue", "issue-safe.json",
                issue_request("issue-safe", "book-safe", "0", [ref("ds", 2, "c")]))

    def test_unknown_and_duplicate_points_rejected(self):
        self.reject("bench-issue", "issue-unknown.json",
                  issue_request("issue", "book", "0", [ref("ds", 9, "x")]),
                  "unknown point")
        self.reject("bench-issue", "issue-dup.json",
                  issue_request("issue", "book", "0",
                                [ref("ds", 1, "a"), ref("ds", 1, "a")]),
                  "distinct")
        self.reject("bench-issue", "issue-empty.json",
                  issue_request("issue", "book", "0", []), "size 1..4096")

    def test_booklet_id_and_release_id_unique(self):
        self.ok("bench-issue", "issue.json",
                issue_request("issue", "book", "0", [ref("ds", 1, "a")]))
        self.reject("bench-issue", "issue-dup.json",
                  issue_request("issue-2", "book", "0", [ref("ds", 2, "c")]),
                  "already registered")
        self.ok("bench-release", "release.json",
                release_request("release", "train", [ref("ds", 1, "b")]))
        self.reject("bench-release", "release-dup.json",
                  release_request("release-2", "train", [ref("ds", 2, "c")]),
                  "already registered")


class RequestNormalisationTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({"ds": {
            (1, "a"): ["0", "0", "1"],
            (1, "b"): ["0", "0", "2"],
            (2, "c"): ["0", "0", "3"],
        }, "ds2": {(4, "d"): ["0", "0", "4"]}})

    def test_point_list_order_and_key_order_are_equivalent(self):
        first = self.ok("bench-release", "release-1.json",
                        release_request("release", "train",
                                        [ref("ds", 1, "a"), ref("ds", 1, "b")]))
        reordered = {
            "points": [
                {"track_id": "b", "sequence": 1, "dataset_id": "ds"},
                {"dataset_id": "ds", "track_id": "a", "sequence": 1},
            ],
            "release_id": "train",
            "op_id": "release",
        }
        second = self.ok("bench-release", "release-2.json", reordered)
        self.assertEqual(first, second)
        self.assertEqual(self.state()["revision"], 1)

    def test_relation_endpoint_order_is_equivalent(self):
        first = self.ok("bench-relate", "relate-1.json",
                        relate_request("relate", [(ref("ds", 1, "a"), ref("ds2", 4, "d"))]))
        swapped = relate_request("relate", [(ref("ds2", 4, "d"), ref("ds", 1, "a"))])
        second = self.ok("bench-relate", "relate-2.json", swapped)
        self.assertEqual(first, second)
        self.assertEqual(self.state()["revision"], 1)

    def test_numeric_forms_compare_by_value(self):
        issued = self.ok("bench-issue", "issue.json",
                         issue_request("issue", "book", "2.0", [ref("ds", 1, "a")]))
        again = self.ok("bench-issue", "issue-again.json",
                        issue_request("issue", "book", "2", [ref("ds", 1, "a")]))
        self.assertEqual(issued, again)

    def test_same_id_different_content_rejected(self):
        self.ok("bench-release", "release-1.json",
                release_request("release", "train", [ref("ds", 1, "a")]))
        self.reject("bench-release", "release-2.json",
                  release_request("release", "train-2", [ref("ds", 1, "b")]),
                  "already used for a different request")
        self.assertEqual(self.state()["revision"], 1)

    def test_prediction_id_unique_within_booklet(self):
        self.ok("bench-issue", "issue.json",
                issue_request("issue", "book", "0", [ref("ds", 1, "a")]))
        sha = self.state()["booklets"]["book"]["sha256"]
        request = score_request("score-1", "book", sha, "pred",
                                [{"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "1"}])
        self.ok("bench-score", "score-1.json", request)
        self.reject("bench-score", "score-2.json",
                  score_request("score-2", "book", sha, "pred",
                                [{"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "1"}]),
                  "already used")


class FailureTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({"ds": {
            (1, "a"): ["0", "0", "1"],
            (1, "b"): ["0", "0", "2"],
            (2, "c"): ["0", "0", "3"],
        }})

    def test_score_rejects_bad_fingerprint_and_coverage(self):
        self.ok("bench-issue", "issue.json",
                issue_request("issue", "book", "0", [ref("ds", 1, "a")]))
        sha = self.state()["booklets"]["book"]["sha256"]
        self.reject("bench-score", "bad-sha.json",
                  score_request("score", "book", "0" * 64, "pred",
                                [{"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "1"}]),
                  "booklet_sha256")
        self.reject("bench-score", "missing.json",
                  score_request("score", "book", sha, "pred", []),
                  "size 1..4096")
        self.reject("bench-score", "extra.json",
                  score_request("score", "book", sha, "pred",
                                [{"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "1"},
                                 {"dataset_id": "ds", "sequence": 1, "track_id": "b", "z": "1"}]),
                  "every booklet point")
        self.reject("bench-score", "dup.json",
                  score_request("score", "book", sha, "pred",
                                [{"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "1"},
                                 {"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "2"}]),
                  "duplicate")
        self.reject("bench-score", "bad-number.json",
                  score_request("score", "book", sha, "pred",
                                [{"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "abc"}]),
                  "bounded decimal")
        # None of these consumed the prediction id.
        self.ok("bench-score", "good.json",
                score_request("score", "book", sha, "pred",
                              [{"dataset_id": "ds", "sequence": 1, "track_id": "a", "z": "1"}]))

    def test_bad_version_same_fingerprint_rejected(self):
        # Rebuild one view with a different version but identical point table.
        points = {(1, "a"): ["0", "0", "1"]}
        document = view_document(points, dataset_id="ds2", version=0)
        path = write_json(self.root / "view-ds2.json", document)
        good = {
            "dataset_id": "ds2", "version": 0, "fingerprint": document["fingerprint"],
        }
        config = {
            "schema_version": 1, "kind": CONFIG_KIND, "project_id": "p",
            "datasets": [good], "relations": [],
        }
        # Claim version 1 while the file (and its fingerprint) say version 0.
        config["datasets"][0]["version"] = 1
        config_path = write_json(self.root / "badversion.json", config)
        result = run_cli("bench", "--config", config_path, "--project",
                         self.root / "other.json", "--view", f"ds2={path}", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match", result.stderr)
        self.assertFalse((self.root / "other.json").exists())

    def test_tampered_view_rejected(self):
        points = {(1, "a"): ["0", "0", "1"]}
        document = view_document(points, dataset_id="ds2", version=0)
        document["points"][0]["xyz"] = ["9", "9", "9"]
        path = write_json(self.root / "tampered.json", document)
        config = {
            "schema_version": 1, "kind": CONFIG_KIND, "project_id": "p",
            "datasets": [{"dataset_id": "ds2", "version": 0,
                          "fingerprint": document["fingerprint"]}],
            "relations": [],
        }
        config_path = write_json(self.root / "tamper-config.json", config)
        result = run_cli("bench", "--config", config_path, "--project",
                         self.root / "other.json", "--view", f"ds2={path}", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("fingerprint", result.stderr)

    def test_relation_batch_is_all_or_nothing(self):
        good = relate_request("relate", [(ref("ds", 1, "a"), ref("ds", 2, "c"))])
        # Unknown endpoint in the second relation poisons the whole batch.
        mixed = relate_request("relate", [
            (ref("ds", 1, "a"), ref("ds", 2, "c")),
            (ref("ds", 1, "b"), ref("ds", 9, "missing")),
        ])
        self.reject("bench-relate", "relate-bad.json", mixed, "unknown point")
        self.assertEqual(self.state()["relations"], [])
        self.assertEqual(self.state()["revision"], 0)
        self.ok("bench-relate", "relate-good.json", good)
        self.assertEqual(len(self.state()["relations"]), 1)
        # A duplicate pair (even reversed) also rejects the whole batch.
        self.reject("bench-relate", "relate-dup.json",
                  relate_request("relate-2", [(ref("ds", 2, "c"), ref("ds", 1, "a"))]),
                  "already registered")
        self.assertEqual(len(self.state()["relations"]), 1)

    def test_initialisation_rejects_duplicate_dataset_and_missing_view(self):
        document = view_document({(1, "a"): ["0", "0", "1"]}, dataset_id="ds")
        path = write_json(self.root / "dup-view.json", document)
        entry = {"dataset_id": "ds", "version": 0,
                 "fingerprint": document["fingerprint"]}
        config = {
            "schema_version": 1, "kind": CONFIG_KIND, "project_id": "p",
            "datasets": [entry, dict(entry)], "relations": [],
        }
        config_path = write_json(self.root / "dup-config.json", config)
        result = run_cli("bench", "--config", config_path, "--project",
                         self.root / "other.json", "--view", f"ds={path}", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate dataset_id", result.stderr)

        config["datasets"] = [entry]
        config_path = write_json(self.root / "missing-config.json", config)
        result = run_cli("bench", "--config", config_path, "--project",
                         self.root / "other.json", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing view", result.stderr)


class ExportTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({"ds": {(1, "a"): ["0", "0", "1"]}})

    def test_export_refuses_existing_target_and_never_half_writes(self):
        self.ok("bench-release", "release.json",
                release_request("release", "train", [ref("ds", 1, "a")]))
        output = self.root / "train.json"
        output.write_bytes(b"already here")
        result = run_cli("bench-export-training", "--project", self.project,
                         "--id", "train", "--output", output, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(output.read_bytes(), b"already here")

        fresh = self.root / "train-ok.json"
        failed = run_cli("bench-export-training", "--project", self.project,
                         "--id", "train", "--output", fresh,
                         fault="benchmark-export-before-write", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(fresh.exists())
        self.assertEqual([p.name for p in self.root.glob(".publish-*")], [])
        run_cli("bench-export-training", "--project", self.project,
                "--id", "train", "--output", fresh)
        self.assertTrue(fresh.exists())

    def test_unknown_material_rejected(self):
        result = run_cli("bench-export-booklet", "--project", self.project,
                         "--id", "nope", "--output", self.root / "x.json", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown booklet_id", result.stderr)


class InterruptionTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({"ds": {
            (1, "a"): ["0", "0", "1"],
            (1, "b"): ["0", "0", "2"],
            (2, "c"): ["0", "0", "3"],
        }})

    def test_interrupt_before_commit_keeps_old_state_then_retry(self):
        self.ok("bench-release", "release.json",
                release_request("release", "train", [ref("ds", 1, "a")]))
        self.ok("bench-issue", "issue.json",
                issue_request("issue", "book", "0", [ref("ds", 2, "c")]))
        self.assertEqual(self.state()["revision"], 2)
        before = self.project.read_bytes()

        # Withdrawing request interrupted before its commit: the material
        # stays open, and the booklet stays comparable.
        failed = self.invoke(
            "bench-relate", "relate.json",
            relate_request("relate", [(ref("ds", 1, "b"), ref("ds", 2, "c"))]),
            fault="benchmark-before-commit", check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected interruption", failed.stderr)
        self.assertEqual(self.project.read_bytes(), before)
        self.assertEqual(self.scores("book")["comparable"], True)

        retried = self.ok("bench-relate", "relate.json",
                          relate_request("relate", [(ref("ds", 1, "b"), ref("ds", 2, "c"))]))
        self.assertEqual(retried["withdrawn"], ["book"])
        self.assertEqual(self.state()["revision"], 3)

    def test_interrupt_after_commit_refuses_leaked_predictions_and_replays(self):
        self.ok("bench-release", "release.json",
                release_request("release", "train", [ref("ds", 1, "a")]))
        self.ok("bench-issue", "issue.json",
                issue_request("issue", "book", "0", [ref("ds", 2, "c")]))
        sha = self.state()["booklets"]["book"]["sha256"]

        failed = self.invoke(
            "bench-relate", "relate.json",
            relate_request("relate", [(ref("ds", 1, "b"), ref("ds", 2, "c"))]),
            fault="benchmark-after-commit", check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        # The withdrawal is durable even though the caller saw a failure.
        status = self.state()
        self.assertEqual(status["revision"], 3)
        self.assertIsNotNone(status["booklets"]["book"]["withdrawal"])

        # No new prediction may slip in for the leaked booklet.
        self.reject("bench-score", "score.json",
                  score_request("score", "book", sha, "pred",
                                [{"dataset_id": "ds", "sequence": 2, "track_id": "c", "z": "3"}]),
                  "withdrawn")

        retry = self.ok("bench-relate", "relate.json",
                        relate_request("relate", [(ref("ds", 1, "b"), ref("ds", 2, "c"))]))
        self.assertEqual(retry["withdrawn"], ["book"])
        self.assertEqual(self.state()["revision"], 3)

        # A later independent operation cannot rewrite the committed history.
        self.ok("bench-release", "release-2.json",
                release_request("release-2", "train-2", [ref("ds", 1, "b")]))
        self.assertEqual(self.audit("book")["booklet"]["withdrawal"]["op_id"], "relate")
        again = self.ok("bench-relate", "relate.json",
                        relate_request("relate", [(ref("ds", 1, "b"), ref("ds", 2, "c"))]))
        self.assertEqual(again, retry)


class ValidationOfStoredStateTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({"ds": {(1, "a"): ["0", "0", "1"]}})

    def test_revision_and_evidence_consistency_checked_on_load(self):
        self.ok("bench-release", "release.json",
                release_request("release", "train", [ref("ds", 1, "a")]))
        document = self.state()
        broken = copy.deepcopy(document)
        broken["revision"] = 99
        write_json(self.project, broken)
        result = run_cli("bench-status", "--project", self.project, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("revision", result.stderr)

        broken = copy.deepcopy(document)
        broken["datasets"]["ds"]["fingerprint"] = "0" * 64
        write_json(self.project, broken)
        result = run_cli("bench-status", "--project", self.project, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("fingerprint", result.stderr)


class CapacityTests(BenchmarkHarness):
    """The 4 x 100 x 200 batch from the acceptance data, end to end."""

    def build_capacity(self):
        datasets = [f"ds{i}" for i in range(4)]
        points = {}
        for index, dataset_id in enumerate(datasets):
            table = {}
            for batch in range(100):
                for item in range(200):
                    table[(batch, f"p{item:03d}")] = [
                        str(batch), str(item), str(1000 + index * 100 + batch)
                    ]
            points[dataset_id] = table
        relations = []
        for dataset_id in datasets:
            for batch in range(99):
                relations.append(
                    (ref(dataset_id, batch, "p000"), ref(dataset_id, batch + 1, "p000"))
                )
        self.make_project(points, relations=relations)
        return datasets

    def test_capacity_batch_withdraws_only_second_view(self):
        datasets = self.build_capacity()
        state = self.state()
        self.assertEqual(state["revision"], 0)
        self.assertEqual(len(state["relations"]), 4 * 99)

        for dataset_id in (datasets[1], datasets[2]):
            booklet_points = [
                ref(dataset_id, batch, f"p{item:03d}")
                for batch in range(100)
                for item in range(200)
            ][:4096]
            self.ok("bench-issue", f"issue-{dataset_id}.json",
                    issue_request(f"issue-{dataset_id}", f"book-{dataset_id}",
                                  "1", booklet_points))

        state = self.state()
        for dataset_id in (datasets[1], datasets[2]):
            booklet = state["booklets"][f"book-{dataset_id}"]
            predictions = []
            for position, (dataset_id_of, sequence, track_id) in enumerate(booklet["points"]):
                xyz = state["datasets"][dataset_id_of]["batches"][str(sequence)]["points"][track_id]
                predictions.append({
                    "dataset_id": dataset_id_of,
                    "sequence": sequence,
                    "track_id": track_id,
                    "z": xyz[2],
                })
            self.ok("bench-score", f"score-{dataset_id}.json",
                    score_request(f"score-{dataset_id}", f"book-{dataset_id}",
                                  booklet["sha256"], f"pred-{dataset_id}", predictions))

        self.ok("bench-release", "release-ds0.json",
                release_request("release-ds0", "train-ds0", [ref("ds0", 0, "p000")]))
        tied = self.ok("bench-relate", "link01.json",
                       relate_request("link01", [(ref("ds1", 0, "p000"), ref("ds0", 0, "p000"))]))
        self.assertEqual(tied["withdrawn"], ["book-ds1"])
        self.assertEqual([row["booklet_id"] for row in self.scores()["scores"]],
                         ["book-ds2"])
        audit = {row["booklet_id"]: row for row in self.audit()["booklets"]}
        self.assertTrue(audit["book-ds1"]["withdrawn"])
        self.assertFalse(audit["book-ds2"]["withdrawn"])
        self.assertEqual(len(audit["book-ds1"]["predictions"]), 1)

        # Both complete booklets re-export byte for byte, without true Z, and
        # every one of the 4096 rows matches the frozen point table exactly.
        for dataset_id in (datasets[1], datasets[2]):
            original, summary = self.export("booklet", f"book-{dataset_id}",
                                            f"book-{dataset_id}.json")
            again, again_summary = self.export("booklet", f"book-{dataset_id}",
                                               f"book-{dataset_id}-again.json")
            self.assertEqual(original.read_bytes(), again.read_bytes())
            self.assertEqual(summary["sha256"], again_summary["sha256"])
            document = read_json(again)
            self.assertEqual(len(document["points"]), 4096)
            self.assertNotIn('"z"', again.read_text(encoding="utf-8"))
            stored = self.state()
            for row in document["points"]:
                batch = stored["datasets"][row["dataset_id"]]["batches"][str(row["sequence"])]
                xyz = batch["points"][row["track_id"]]
                self.assertEqual(row["xy"], [xyz[0], xyz[1]])
                self.assertEqual(row["source"], {
                    "dataset_id": row["dataset_id"],
                    "version": 0,
                    "fingerprint": stored["datasets"][row["dataset_id"]]["fingerprint"],
                })

        # The training package is exactly the one published point, with full
        # XYZ, and the frozen point table never changed.
        training, training_summary = self.export("training", "train-ds0", "train-ds0.json")
        package = read_json(training)
        self.assertEqual(package["count"], 1)
        self.assertEqual(package["points"][0]["track_id"], "p000")
        self.assertEqual(package["points"][0]["xyz"],
                         self.state()["datasets"]["ds0"]["batches"]["0"]["points"]["p000"])
        self.assertEqual(training_summary["sha256"],
                         hashlib.sha256(training.read_bytes()).hexdigest())

        # Old receipts replay unchanged and never advance the revision.
        revision = self.state()["revision"]
        released_replay = self.ok("bench-release", "release-ds0.json",
                                  release_request("release-ds0", "train-ds0",
                                                  [ref("ds0", 0, "p000")]))
        self.assertEqual(released_replay["withdrawn"], [])
        tied_replay = self.ok("bench-relate", "link01.json",
                              relate_request("link01", [(ref("ds1", 0, "p000"),
                                                         ref("ds0", 0, "p000"))]))
        self.assertEqual(tied_replay, tied)
        self.assertEqual(self.state()["revision"], revision)


class RealDeliveryChainTests(BenchmarkHarness):
    """One walkthrough using a view actually emitted by the delivery chain."""

    def test_view_from_delivery_chain_feeds_benchmark(self):
        build = {
            "dataset_id": "chain",
            "max_reprojection_error": "0",
            "cameras": [LEFT, RIGHT],
            "batches": [
                {"sequence": 10, "tracks": [{"id": "marker", "observations": [
                    {"camera_id": "left", "xy": ["0.5", "0.25"], "weight": "1"},
                    {"camera_id": "right", "xy": ["0.25", "0.25"], "weight": "1"},
                ]}]},
            ],
        }
        build_path = write_json(self.root / "build.json", build)
        store = self.root / "up.json"
        run_cli("build", "--input", build_path, "--store", store)
        full = self.root / "full.json"
        run_cli("export", "--store", store, "--full", "--output", full)
        down = self.root / "down.json"
        run_cli("apply", "--store", down, "--package", full)
        view = self.root / "chain-view.json"
        run_cli("view", "--store", down, "--output", view)
        document = read_json(view)
        config = {
            "schema_version": 1, "kind": CONFIG_KIND, "project_id": "chain-bench",
            "datasets": [{"dataset_id": document["dataset_id"],
                          "version": document["version"],
                          "fingerprint": document["fingerprint"]}],
            "relations": [],
        }
        config_path = write_json(self.root / "chain-config.json", config)
        run_cli("bench", "--config", config_path, "--project", self.project,
                "--view", f"{document['dataset_id']}={view}")
        self.assertEqual(self.state()["datasets"]["chain"]["batches"]["10"]["points"]["marker"],
                         ["2", "1", "4"])


if __name__ == "__main__":
    unittest.main()


class MultiWithdrawalTests(BenchmarkHarness):
    def setUp(self):
        super().setUp()
        self.make_project({
            "dsA": {(1, "a"): ["0", "0", "1"], (1, "b"): ["0", "0", "2"],
                    (2, "c"): ["0", "0", "3"], (5, "e"): ["0", "0", "5"]},
            "dsB": {(3, "d"): ["0", "0", "4"]},
        })

    def test_one_operation_withdraws_every_affected_booklet(self):
        for name, point in (("book-1", ref("dsA", 2, "c")),
                            ("book-2", ref("dsB", 3, "d")),
                            ("book-3", ref("dsA", 5, "e"))):
            self.ok("bench-issue", f"{name}.json",
                    issue_request(f"issue-{name}", name, "0", [point]))
        for name in ("book-1", "book-2"):
            sha = self.state()["booklets"][name]["sha256"]
            ref_point = self.state()["booklets"][name]["points"][0]
            self.ok("bench-score", f"score-{name}.json",
                    score_request(f"score-{name}", name, sha, f"pred-{name}",
                                  [{"dataset_id": ref_point[0], "sequence": ref_point[1],
                                    "track_id": ref_point[2], "z": "0"}]))
        self.ok("bench-release", "release.json",
                release_request("release", "train", [ref("dsA", 1, "a")]))
        # One late relation chain reaches both book-1 and book-3; book-2 is
        # untouched.  All newly affected booklets are listed in one receipt.
        receipt = self.ok("bench-relate", "relate.json",
                          relate_request("relate", [
                              (ref("dsA", 1, "b"), ref("dsA", 2, "c")),
                              (ref("dsA", 1, "b"), ref("dsA", 5, "e")),
                          ]))
        self.assertEqual(receipt["withdrawn"], ["book-1", "book-3"])
        self.assertEqual([row["booklet_id"] for row in self.scores()["scores"]],
                         ["book-2"])
        self.assertFalse(self.scores("book-1")["comparable"])
        self.assertTrue(self.scores("book-2")["comparable"])

    def test_self_loop_and_duplicate_cross_batch_rejected_whole(self):
        for document, needle in (
            (relate_request("r1", [(ref("dsA", 1, "a"), ref("dsA", 1, "a"))]),
             "two different points"),
            (relate_request("r2", [(ref("dsA", 1, "a"), ref("dsB", 3, "d")),
                                   (ref("dsB", 3, "d"), ref("dsA", 1, "a"))]),
             "duplicate relation"),
        ):
            self.reject("bench-relate", f"{document['op_id']}.json", document, needle)
        self.assertEqual(self.state()["relations"], [])
        self.assertEqual(self.state()["revision"], 0)
        # A wholly valid batch is then accepted.
        self.ok("bench-relate", "good.json",
                relate_request("good", [(ref("dsA", 1, "a"), ref("dsB", 3, "d"))]))
        self.assertEqual(len(self.state()["relations"]), 1)


class EvidencePreferenceTests(BenchmarkHarness):
    def test_evidence_prefers_a_distinct_training_point(self):
        self.make_project({
            "ds": {(1, "a"): ["0", "0", "1"], (1, "b"): ["0", "0", "2"],
                   (2, "c"): ["0", "0", "3"]},
        })
        # Publish a and b from the same batch; c is the booklet point.
        self.ok("bench-release", "release.json",
                release_request("release", "train",
                                [ref("ds", 1, "a"), ref("ds", 1, "b")]))
        self.ok("bench-issue", "issue.json",
                issue_request("issue", "book", "0", [ref("ds", 2, "c")]))
        self.ok("bench-relate", "relate.json",
                relate_request("relate", [(ref("ds", 1, "a"), ref("ds", 2, "c"))]))
        evidence = self.audit("book")["booklet"]["withdrawal"]
        self.assertEqual(evidence["test_point"], ref("ds", 2, "c"))
        # The batch is exposed by both a and b; a distinct published point is
        # chosen so the evidence is not the test point itself.
        self.assertNotEqual(evidence["training_point"], evidence["test_point"])
        self.assertIn(evidence["training_point"], (ref("ds", 1, "a"), ref("ds", 1, "b")))
        self.assertEqual(evidence["release_id"], "train")
