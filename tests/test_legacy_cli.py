import json
from pathlib import Path
import tempfile
import unittest

from helpers import ROOT, read_json, run_cli, write_json


class LegacyEntryPointTests(unittest.TestCase):
    """The original single-dataset command keeps its input, report and codes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = ROOT / "fixtures" / "input.json"

    def tearDown(self):
        self.directory.cleanup()

    def run_legacy(self, source, output, check=False):
        return run_cli("--source", source, "--output", output, check=check)

    def test_report_shape_and_summary(self):
        output = self.root / "report.json"
        result = self.run_legacy(self.source, output, check=True)
        self.assertEqual(
            json.loads(result.stdout),
            {"cameras": 2, "reconstructed": 1, "tracks": 1,
             "tracks_with_nonpositive_depth": 0, "unobservable": 0},
        )
        report = read_json(output)
        self.assertEqual(report["schema_version"], 1)
        track = report["tracks"][0]
        self.assertEqual(track["track_id"], "marker")
        self.assertEqual(track["status"], "reconstructed")
        self.assertEqual(track["xyz"], ["2", "1", "4"])
        self.assertEqual(track["algebraic_squared_error"], "0")
        self.assertEqual(track["normal_equation_residual"], ["0", "0", "0"])
        self.assertEqual(
            [(p["camera_id"], p["depth"], p["xy"], p["residual_xy"])
             for p in track["reprojections"]],
            [("left", "4", ["1/2", "1/4"], ["0", "0"]),
             ("right", "4", ["1/4", "1/4"], ["0", "0"])],
        )

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "report.json"
        output.write_text("sentinel", encoding="utf-8")
        result = self.run_legacy(self.source, output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")

    def test_invalid_input_leaves_no_report(self):
        bad = self.root / "bad.json"
        bad.write_text('{"cameras": [], "tracks": []}', encoding="utf-8")
        output = self.root / "report.json"
        result = self.run_legacy(bad, output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("array", result.stderr)
        self.assertFalse(output.exists())

    def test_duplicate_keys_and_rank_failures_still_rejected(self):
        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            '{"cameras":[],"tracks":[],"cameras":[]}', encoding="utf-8"
        )
        self.assertEqual(self.run_legacy(duplicate, self.root / "a.json").returncode, 2)

        document = read_json(self.source)
        document["cameras"][0]["projection"][2] = ["0", "0", "0", "0"]
        rank = write_json(self.root / "rank.json", document)
        self.assertEqual(self.run_legacy(rank, self.root / "b.json").returncode, 2)

    def test_business_failure_still_exits_zero(self):
        document = read_json(self.source)
        document["cameras"][1]["projection"] = document["cameras"][0]["projection"]
        document["tracks"][0]["observations"][1]["xy"] = ["0.5", "0.25"]
        path = write_json(self.root / "unobservable.json", document)
        output = self.root / "report.json"
        result = self.run_legacy(path, output, check=True)
        self.assertEqual(json.loads(result.stdout)["unobservable"], 1)
        self.assertEqual(read_json(output)["tracks"][0]["status"], "unobservable")

    def test_unknown_subcommand_style_arguments_are_errors(self):
        result = run_cli("--source", self.source, "--output", self.root / "x.json",
                         "--version", 0, check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
