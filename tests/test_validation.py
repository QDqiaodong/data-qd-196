import json
from pathlib import Path
import tempfile
import unittest

from helpers import build_document, read_json, run_cli, write_json


class BuildValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def build(self, document, name="build.json", check=False):
        path = write_json(self.root / name, document)
        store = self.root / f"{name}.state.json"
        result = run_cli("build", "--input", path, "--store", store, check=check)
        return result, store

    def test_store_is_self_contained_after_input_removal(self):
        document = build_document()
        result, store = self.build(document, check=True)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["version"], summary["points"]), (0, 3))
        path = self.root / "build.json"
        path.unlink()
        queried = json.loads(
            run_cli("query", "--store", store, "--version", 0, "--sequence", 10,
                    "--track", "marker").stdout
        )
        self.assertEqual(queried["xyz"], ["2", "1", "4"])

    def test_batch_and_threshold_bounds(self):
        document = build_document()
        document["batches"] = []
        self.assertNotEqual(self.build(document, "empty.json")[0].returncode, 0)

        document = build_document()
        document["batches"] = document["batches"] * 34  # 102 batches
        for index, batch in enumerate(document["batches"]):
            batch["sequence"] = index
        self.assertNotEqual(self.build(document, "many.json")[0].returncode, 0)

        document = build_document()
        document["batches"][1]["sequence"] = 10
        self.assertIn("duplicate", self.build(document, "dupseq.json")[0].stderr)

        document = build_document()
        document["batches"][0]["sequence"] = -1
        self.assertNotEqual(self.build(document, "neg.json")[0].returncode, 0)

        document = build_document()
        document["batches"][0]["sequence"] = 10**9 + 1
        self.assertNotEqual(self.build(document, "big.json")[0].returncode, 0)

        document = build_document()
        document["max_reprojection_error"] = "-1"
        result = self.build(document, "negthr.json")[0]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-negative", result.stderr)

        document = build_document()
        document["max_reprojection_error"] = 0.5
        self.assertNotEqual(self.build(document, "numthr.json")[0].returncode, 0)

    def test_no_failed_build_leaves_a_store(self):
        document = build_document()
        document["cameras"][0]["projection"][0][0] = 1  # not a decimal string
        result, store = self.build(document, "bad.json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(store.exists())

    def test_build_refuses_existing_store(self):
        document = build_document()
        result, store = self.build(document, check=True)
        second, _ = self.build(document, check=False)
        self.assertEqual(second.returncode, 2)
        self.assertIn("already exists", second.stderr)

    def test_interrupted_build_publishes_nothing(self):
        path = write_json(self.root / "build.json", build_document())
        store = self.root / "state.json"
        result = run_cli("build", "--input", path, "--store", store,
                         fault="build-before-commit", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(store.exists())

    def test_duplicate_track_id_within_a_batch_rejected(self):
        document = build_document(sequences=(10,))
        document["batches"][0]["tracks"].append(
            {"id": "marker", "observations": [
                {"camera_id": "left", "xy": ["0.5", "0.25"], "weight": "1"},
                {"camera_id": "right", "xy": ["0.25", "0.25"], "weight": "1"},
            ]}
        )
        self.assertNotEqual(self.build(document, "duptrack.json")[0].returncode, 0)

    def test_too_many_replacements_rejected(self):
        document = build_document()
        result, store = self.build(document, check=True)
        request = {
            "dataset_id": "site-a", "change_id": "many", "base_version": 0,
            "replacements": [
                {"camera_id": "right", "start": index, "end": index,
                 "projection": [["1", "0", "0", "-1"], ["0", "1", "0", "0"],
                                ["0", "0", "1", "0"]]}
                for index in range(21)
            ],
        }
        path = write_json(self.root / "many-replacements.json", request)
        outcome = run_cli("correct", "--store", store, "--request", path, check=False)
        self.assertEqual(outcome.returncode, 2)
        self.assertIn("1..20", outcome.stderr)

    def test_tampered_store_is_rejected_not_silently_used(self):
        document = build_document()
        _, store = self.build(document, check=True)
        state = read_json(store)
        state["delivery"]["points"]["10/marker"] = ["9", "9", "9"]
        state["delivery"]["fingerprint"] = state["history"][0]["fingerprint"]
        tampered = write_json(self.root / "tampered.json", state)
        outcome = run_cli("query", "--store", tampered, "--version", 0,
                          "--sequence", 10, "--track", "marker", check=False)
        self.assertEqual(outcome.returncode, 2)
        self.assertIn("fingerprint", outcome.stderr)

    def test_invalid_utf8_and_nonfinite_rejected(self):
        raw = self.root / "latin1.json"
        raw.write_bytes(b'{"dataset_id":"x","max_reprojection_error":"0","cameras":[],'
                        b'"batches":[],"caf\xe9":1}')
        outcome = run_cli("build", "--input", raw, "--store", self.root / "s.json",
                          check=False)
        self.assertEqual(outcome.returncode, 2)

        nonfinite = self.root / "nan.json"
        nonfinite.write_text(
            '{"dataset_id":"x","max_reprojection_error":"0","cameras":[],'
            '"batches":[],"v":NaN}', encoding="utf-8")
        outcome = run_cli("build", "--input", nonfinite, "--store", self.root / "s2.json",
                          check=False)
        self.assertEqual(outcome.returncode, 2)
        self.assertIn("nonfinite", outcome.stderr)

    def test_same_track_id_in_different_batches_is_distinct(self):
        document = build_document(sequences=(10, 20))
        result, store = self.build(document, check=True)
        self.assertEqual(json.loads(result.stdout)["points"], 2)
        state = read_json(store)
        self.assertEqual(set(state["delivery"]["points"]), {"10/marker", "20/marker"})


if __name__ == "__main__":
    unittest.main()
