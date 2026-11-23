"""Earthwork (土方) package tests."""

import json
from pathlib import Path
import random
import resource
import sys
import tempfile
import time
import unittest

from fractions import Fraction

from helpers import ROOT, SRC, measure_cli, read_json, run_cli, write_json

sys.path.insert(0, str(SRC))
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402

VIEW_KIND = "camera-tracks-view"
PLAN_KIND = "camera-tracks-earthwork-plan"
PACKAGE_KIND = "camera-tracks-earthwork"


def view_document(points, dataset_id, version=0):
    """A view export equivalent, straight from the canonical point table."""
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


def endpoint(view):
    return {
        "dataset_id": view["dataset_id"],
        "version": view["version"],
        "fingerprint": view["fingerprint"],
    }


def ref(sequence, track_id):
    return {"sequence": sequence, "track_id": track_id}


def block(block_id, area, lower_left, lower_right, upper_left):
    return {
        "block_id": block_id,
        "area": list(area),
        "lower_left": lower_left,
        "lower_right": lower_right,
        "upper_left": upper_left,
    }


def plan_document(zone, old_view, new_view, old_blocks, new_blocks,
                  zone_id="zone-196"):
    return {
        "schema_version": 1,
        "kind": PLAN_KIND,
        "zone_id": zone_id,
        "zone": list(zone),
        "old": {**endpoint(old_view), "blocks": old_blocks},
        "new": {**endpoint(new_view), "blocks": new_blocks},
    }


def base_views():
    """Old ground z = 0 and new ground z = x - 3/2, same three identities."""
    old = view_document(
        {
            (10, "g"): ["0", "0", "0"],
            (20, "g"): ["2", "0", "0"],
            (30, "g"): ["0", "1", "0"],
        },
        "old-ground",
    )
    new = view_document(
        {
            (10, "g"): ["1", "0", "-1/2"],
            (20, "g"): ["3", "0", "3/2"],
            (30, "g"): ["1", "1", "-1/2"],
        },
        "new-ground",
    )
    return old, new


def base_blocks():
    return (
        [
            block(
                "old-a", (0, 0, 2, 1),
                ref(10, "g"), ref(20, "g"), ref(30, "g"),
            )
        ],
        [
            block(
                "new-b", (1, 0, 3, 1),
                ref(10, "g"), ref(20, "g"), ref(30, "g"),
            )
        ],
    )


class EarthworkCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def write_inputs(self, plan, old, new, prefix="case"):
        paths = {
            "old": write_json(self.root / f"{prefix}-old.json", old),
            "new": write_json(self.root / f"{prefix}-new.json", new),
            "plan": write_json(self.root / f"{prefix}-plan.json", plan),
        }
        return paths

    def earthwork(self, plan, old, new, output=None, prefix="case", check=True):
        paths = self.write_inputs(plan, old, new, prefix)
        if output is None:
            output = self.root / f"{prefix}-package.json"
        result = run_cli(
            "earthwork", "--old", paths["old"], "--new", paths["new"],
            "--plan", paths["plan"], "--output", output, check=False,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
            return read_json(output), result, output
        return None, result, output

    def tampered_view(self, view, mutate):
        """Apply ``mutate`` to the points and refresh the fingerprint."""
        points = {
            (entry["sequence"], entry["track_id"]): list(entry["xyz"])
            for entry in view["points"]
        }
        mutate(points)
        return view_document(
            points, view["dataset_id"], view["version"]
        )


# ---------------------------------------------------------------------------
# The documented walkthrough and its variants


class SampleTests(EarthworkCase):
    def test_primary_zone_settles_one_slice_with_cut_and_fill(self):
        old, new = base_views()
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        package, result, output = self.earthwork(plan, old, new)
        summary = json.loads(result.stdout)
        self.assertEqual(
            {key: summary[key] for key in
             ("slices", "settled_area", "unsettled_area", "settled_cut",
              "settled_fill", "status", "missing_blocks")},
            {"slices": 1, "settled_area": "1", "unsettled_area": "2",
             "settled_cut": "1/8", "settled_fill": "1/8",
             "status": "partial", "missing_blocks": 0},
        )
        self.assertEqual(package["kind"], PACKAGE_KIND)
        self.assertEqual(package["zone_id"], "zone-196")
        self.assertEqual(package["zone"], [0, 0, 3, 1])
        self.assertEqual(package["zone_area"], "3")
        self.assertEqual(package["slice_count"], 1)
        self.assertEqual(package["missing_blocks"], [])
        self.assertEqual(package["settled_area"], "1")
        self.assertEqual(package["unsettled_area"], "2")
        self.assertEqual(package["status"], "partial")
        self.assertEqual(package["settled_cut"], "1/8")
        self.assertEqual(package["settled_fill"], "1/8")
        self.assertEqual(package["old"], endpoint(old))
        self.assertEqual(package["new"], endpoint(new))
        (entry,) = package["slices"]
        self.assertEqual(entry["range"], [1, 0, 2, 1])
        self.assertEqual((entry["old_block"], entry["new_block"]),
                         ("old-a", "new-b"))
        self.assertEqual(
            entry["corner_differences"],
            {"lower_left": "-1/2", "lower_right": "1/2",
             "upper_right": "1/2", "upper_left": "-1/2"},
        )
        self.assertEqual(entry["cut"], "1/8")
        self.assertEqual(entry["fill"], "1/8")
        # Independently recompute the two volumes from the corner differences
        # by integrating the affine difference over the slice.
        cut, fill = _recompute(entry)
        self.assertLess(abs(cut - Fraction(1, 8)), Fraction(1, 100))
        self.assertLess(abs(fill - Fraction(1, 8)), Fraction(1, 100))
        self.assertEqual(output.exists(), True)

    def test_missing_new_anchor_disqualifies_whole_block(self):
        old, new = base_views()
        new = self.tampered_view(
            new, lambda points: points.pop((20, "g"))
        )
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(plan, old, new, prefix="gap")
        self.assertEqual(package["slices"], [])
        self.assertEqual(package["settled_area"], "0")
        self.assertEqual(package["unsettled_area"], "3")
        self.assertEqual(package["status"], "partial")
        self.assertEqual(package["settled_cut"], "0")
        self.assertEqual(package["settled_fill"], "0")
        self.assertEqual(len(package["missing_blocks"]), 1)
        missing = package["missing_blocks"][0]
        self.assertEqual(missing["layer"], "new")
        self.assertEqual(missing["block_id"], "new-b")
        self.assertEqual(missing["area"], [1, 0, 3, 1])
        self.assertEqual(
            missing["missing_anchors"],
            [{"corner": "lower_right", "sequence": 20, "track_id": "g"}],
        )

    def test_missing_anchor_keeps_unaffected_slices(self):
        old = view_document(
            {
                (10, "a"): ["0", "0", "0"],
                (20, "a"): ["1", "0", "0"],
                (30, "a"): ["0", "1", "0"],
                (40, "a"): ["2", "0", "0"],
                (50, "a"): ["1", "1", "0"],
            },
            "old-ground",
        )
        new = view_document(
            {
                (10, "b"): ["0", "0", "1"],
                (20, "b"): ["1", "0", "1"],
                (30, "b"): ["0", "1", "1"],
                (40, "b"): ["2", "0", "1"],
                (50, "b"): ["1", "1", "1"],
            },
            "new-ground",
        )
        old_blocks = [
            block("oa", (0, 0, 1, 1),
                  ref(10, "a"), ref(20, "a"), ref(30, "a")),
            block("ob", (1, 0, 2, 1),
                  ref(20, "a"), ref(40, "a"), ref(50, "a")),
        ]
        new_blocks = [
            block("na", (0, 0, 1, 1),
                  ref(10, "b"), ref(20, "b"), ref(30, "b")),
            block("nb", (1, 0, 2, 1),
                  ref(20, "b"), ref(40, "b"), ref(50, "b")),
        ]
        plan = plan_document((0, 0, 2, 1), old, new, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(plan, old, new, prefix="keep")
        self.assertEqual([entry["range"] for entry in package["slices"]],
                         [[0, 0, 1, 1], [1, 0, 2, 1]])
        self.assertEqual(package["settled_area"], "2")
        self.assertEqual(package["unsettled_area"], "0")
        self.assertEqual(package["status"], "complete")
        # Both slices are 1 m higher everywhere: volume 1 each.
        self.assertEqual(package["settled_fill"], "2")
        self.assertEqual(package["settled_cut"], "0")

        # Delete one anchor of the second new block; the first slice survives.
        broken = self.tampered_view(new, lambda points: points.pop((40, "b")))
        plan = plan_document((0, 0, 2, 1), old, broken, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(
            plan, old, broken, prefix="keep-gap"
        )
        self.assertEqual([entry["range"] for entry in package["slices"]],
                         [[0, 0, 1, 1]])
        self.assertEqual(package["settled_area"], "1")
        self.assertEqual(package["unsettled_area"], "1")
        self.assertEqual([entry["block_id"] for entry in package["missing_blocks"]],
                         ["nb"])
        self.assertEqual(package["slices"][0]["old_block"], "oa")
        self.assertEqual(package["slices"][0]["new_block"], "na")

    def test_same_height_full_coverage_is_complete(self):
        old = view_document(
            {(10, "p"): ["0", "0", "0"], (20, "p"): ["2", "0", "0"],
             (30, "p"): ["0", "1", "0"]}, "old")
        new = view_document(
            {(10, "q"): ["0", "0", "0"], (20, "q"): ["2", "0", "0"],
             (30, "q"): ["0", "1", "0"]}, "new")
        old_blocks = [block("o", (0, 0, 2, 1),
                            ref(10, "p"), ref(20, "p"), ref(30, "p"))]
        new_blocks = [block("n", (0, 0, 2, 1),
                            ref(10, "q"), ref(20, "q"), ref(30, "q"))]
        plan = plan_document((0, 0, 2, 1), old, new, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(plan, old, new, prefix="flat")
        self.assertEqual(len(package["slices"]), 1)
        entry = package["slices"][0]
        self.assertEqual(entry["range"], [0, 0, 2, 1])
        self.assertEqual(set(entry["corner_differences"].values()), {"0"})
        self.assertEqual((entry["cut"], entry["fill"]), ("0", "0"))
        self.assertEqual(package["settled_area"], "2")
        self.assertEqual(package["unsettled_area"], "0")
        self.assertEqual(package["status"], "complete")

    def test_blocks_touching_only_at_an_edge_form_no_slice(self):
        old = view_document(
            {(10, "p"): ["0", "0", "0"], (20, "p"): ["1", "0", "0"],
             (30, "p"): ["0", "1", "0"]}, "old")
        new = view_document(
            {(10, "q"): ["1", "0", "1"], (20, "q"): ["2", "0", "1"],
             (30, "q"): ["1", "1", "1"]}, "new")
        old_blocks = [block("o", (0, 0, 1, 1),
                            ref(10, "p"), ref(20, "p"), ref(30, "p"))]
        new_blocks = [block("n", (1, 0, 2, 1),
                            ref(10, "q"), ref(20, "q"), ref(30, "q"))]
        plan = plan_document((0, 0, 2, 1), old, new, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(plan, old, new, prefix="edge")
        self.assertEqual(package["slices"], [])
        self.assertEqual(package["settled_area"], "0")
        self.assertEqual(package["unsettled_area"], "2")
        self.assertEqual(package["status"], "partial")

    def test_zero_on_an_edge_creates_no_cut(self):
        old = view_document(
            {(10, "p"): ["0", "0", "0"], (20, "p"): ["1", "0", "0"],
             (30, "p"): ["0", "1", "0"]}, "old")
        new = view_document(
            {(10, "q"): ["0", "0", "0"], (20, "q"): ["1", "0", "0"],
             (30, "q"): ["0", "1", "1"]}, "new")
        old_blocks = [block("o", (0, 0, 1, 1),
                            ref(10, "p"), ref(20, "p"), ref(30, "p"))]
        new_blocks = [block("n", (0, 0, 1, 1),
                            ref(10, "q"), ref(20, "q"), ref(30, "q"))]
        plan = plan_document((0, 0, 1, 1), old, new, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(plan, old, new, prefix="zero")
        (entry,) = package["slices"]
        self.assertEqual(entry["corner_differences"]["lower_left"], "0")
        self.assertEqual(entry["corner_differences"]["lower_right"], "0")
        self.assertEqual(entry["cut"], "0")
        self.assertEqual(entry["fill"], "1/2")
        self.assertEqual(package["status"], "complete")

    def test_zero_line_through_corners_splits_both_ways(self):
        old = view_document(
            {(10, "p"): ["0", "0", "0"], (20, "p"): ["1", "0", "0"],
             (30, "p"): ["0", "1", "0"]}, "old")
        new = view_document(
            {(10, "q"): ["0", "0", "-1"], (20, "q"): ["1", "0", "0"],
             (30, "q"): ["0", "1", "0"]}, "new")
        old_blocks = [block("o", (0, 0, 1, 1),
                            ref(10, "p"), ref(20, "p"), ref(30, "p"))]
        new_blocks = [block("n", (0, 0, 1, 1),
                            ref(10, "q"), ref(20, "q"), ref(30, "q"))]
        plan = plan_document((0, 0, 1, 1), old, new, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(plan, old, new, prefix="line")
        (entry,) = package["slices"]
        # z_new - z_old = x + y - 1: two triangles, each 1/6 m^3.
        self.assertEqual(entry["cut"], "1/6")
        self.assertEqual(entry["fill"], "1/6")
        self.assertEqual(package["settled_cut"], "1/6")
        self.assertEqual(package["settled_fill"], "1/6")
        self.assertEqual(package["settled_area"], "1")
        self.assertEqual(package["unsettled_area"], "0")
        self.assertEqual(package["status"], "complete")


def _recompute(entry, steps=400):
    """Recompute cut/fill from the slice range and four corner differences.

    This deliberately uses a different method from the implementation: a
    midpoint Riemann sum over the bilinear-interpolated difference.  Midpoint
    quadrature is exact for an affine function on a cell that does not cross
    the zero line, so the only error is the sub-cell handling of the zero
    line; the comparison is therefore tolerant, while the package's own
    exact values are asserted directly elsewhere.
    """
    left, bottom, right, top = entry["range"]
    corners = entry["corner_differences"]
    ll = Fraction(corners["lower_left"])
    lr = Fraction(corners["lower_right"])
    ul = Fraction(corners["upper_left"])
    a = (lr - ll) / (right - left)
    b = (ul - ll) / (top - bottom)
    c = ll - a * left - b * bottom

    dx = Fraction(right - left, steps)
    dy = Fraction(top - bottom, steps)
    cut = fill = Fraction(0)
    for i in range(steps):
        x = left + (i + Fraction(1, 2)) * dx
        for j in range(steps):
            y = bottom + (j + Fraction(1, 2)) * dy
            amount = (a * x + b * y + c) * dx * dy
            if amount > 0:
                fill += amount
            elif amount < 0:
                cut -= amount
    return cut, fill


# ---------------------------------------------------------------------------
# Invalid plans and endpoints


class ValidationTests(EarthworkCase):
    def assert_rejected(self, plan, old, new, needle=None, prefix="bad"):
        _package, result, output = self.earthwork(
            plan, old, new, prefix=prefix, check=False
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertTrue(result.stderr.lstrip().startswith("error:"), result.stderr)
        if needle is not None:
            self.assertIn(needle, result.stderr)
        self.assertFalse(output.exists(), "invalid input produced a package")

    def test_overlapping_interiors_rejected_even_with_missing_anchor(self):
        old, new = base_views()
        old_blocks, new_blocks = base_blocks()
        # A second old block overlapping the first, missing a corner in the
        # view.  The overlap is illegal regardless of the missing anchor.
        old_blocks = old_blocks + [
            block("old-c", (1, 0, 2, 1),
                  ref(10, "g"), ref(20, "g"), ref(99, "g"))
        ]
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        self.assert_rejected(plan, old, new, "overlapping", prefix="ov")

    def test_blocks_sharing_only_an_edge_are_legal(self):
        old = view_document(
            {(10, "p"): ["0", "0", "0"], (20, "p"): ["1", "0", "0"],
             (30, "p"): ["0", "1", "0"], (31, "p"): ["2", "0", "0"],
             (32, "p"): ["1", "1", "0"]}, "old")
        new = view_document(
            {(10, "q"): ["0", "0", "1"], (20, "q"): ["1", "0", "1"],
             (30, "q"): ["0", "1", "1"], (31, "q"): ["2", "0", "1"],
             (32, "q"): ["1", "1", "1"]}, "new")
        old_blocks = [
            block("o1", (0, 0, 1, 1),
                  ref(10, "p"), ref(20, "p"), ref(30, "p")),
            block("o2", (1, 0, 2, 1),
                  ref(20, "p"), ref(31, "p"), ref(32, "p")),
        ]
        new_blocks = [
            block("n1", (0, 0, 2, 1),
                  ref(10, "q"), ref(31, "q"), ref(30, "q"))
        ]
        plan = plan_document((0, 0, 2, 1), old, new, old_blocks, new_blocks)
        package, _result, _output = self.earthwork(
            plan, old, new, prefix="touch"
        )
        self.assertEqual(package["settled_area"], "2")
        self.assertEqual(package["unsettled_area"], "0")
        self.assertEqual(package["status"], "complete")
        self.assertEqual(sorted(entry["old_block"] for entry in package["slices"]),
                         ["o1", "o2"])

    def test_missing_corner_does_not_hide_a_wrong_corner_xy(self):
        old, new = base_views()

        def break_anchor(points):
            points.pop((10, "g"))  # lower_left is missing
            points[(20, "g")] = ["5/2", "0", "3/2"]  # lower_right XY is wrong

        broken = self.tampered_view(new, break_anchor)
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, broken, old_blocks, new_blocks)
        self.assert_rejected(plan, old, broken, "declared corner", prefix="xy")

    def test_present_anchor_must_match_the_declared_corner(self):
        old, new = base_views()
        broken = self.tampered_view(
            new, lambda points: points.__setitem__((20, "g"), ["0", "0", "3/2"])
        )
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, broken, old_blocks, new_blocks)
        self.assert_rejected(plan, old, broken, "new-b", prefix="coord")

    def test_same_fingerprint_does_not_excuse_a_different_version(self):
        old, new = base_views()
        newer = dict(new)
        newer["version"] = 1
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, newer, old_blocks, new_blocks)
        plan["new"]["version"] = 0  # plan still pins version 0
        self.assert_rejected(plan, old, newer, "version", prefix="version")

        # The plan itself can point at a version the file really has, but a
        # different fingerprint is still refused.
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        plan["new"]["fingerprint"] = "0" * 64
        self.assert_rejected(plan, old, new, "fingerprint", prefix="finger")

    def test_tampered_point_table_is_rejected(self):
        old, new = base_views()
        new = json.loads(json.dumps(new))
        new["points"][0]["xyz"] = ["1", "0", "99"]
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        # Keep the plan pinned to the file's own (stale) fingerprint.
        plan["new"]["fingerprint"] = new["fingerprint"]
        self.assert_rejected(plan, old, new, "fingerprint", prefix="tamper")

    def test_zone_and_block_geometry_are_bounded(self):
        old, new = base_views()
        old_blocks, new_blocks = base_blocks()
        for zone, needle in (
            ((0, 0, 0, 1), "positive area"),
            ((0, 0, 3, -1), "positive area"),
        ):
            plan = plan_document(zone, old, new, old_blocks, new_blocks)
            self.assert_rejected(plan, old, new, needle, prefix="zone")

        outside = [block("o", (0, 0, 4, 1),
                         ref(10, "g"), ref(20, "g"), ref(30, "g"))]
        plan = plan_document((0, 0, 3, 1), old, new, outside, new_blocks)
        self.assert_rejected(plan, old, new, "inside the work zone",
                             prefix="outside")

        flat = [block("o", (0, 0, 0, 1),
                      ref(10, "g"), ref(20, "g"), ref(30, "g"))]
        plan = plan_document((0, 0, 3, 1), old, new, flat, new_blocks)
        self.assert_rejected(plan, old, new, "positive area", prefix="flat")

        huge = [block("o", (-10**9 - 1, 0, 0, 1),
                      ref(10, "g"), ref(20, "g"), ref(30, "g"))]
        plan = plan_document((-10**9, 0, 3, 1), old, new, huge, new_blocks)
        self.assert_rejected(plan, old, new, prefix="huge")

    def test_empty_layers_settle_nothing(self):
        old, new = base_views()
        plan = plan_document((0, 0, 3, 1), old, new, [], [])
        package, _result, _output = self.earthwork(
            plan, old, new, prefix="empty"
        )
        self.assertEqual(package["slices"], [])
        self.assertEqual(package["slice_count"], 0)
        self.assertEqual(package["settled_area"], "0")
        self.assertEqual(package["unsettled_area"], "3")
        self.assertEqual(package["status"], "partial")
        self.assertEqual(package["missing_blocks"], [])

    def test_block_limit_is_enforced_per_layer(self):
        old, new = base_views()
        too_many = [
            block(f"b{i}", (0, 0, 1, 1),
                  ref(10, "g"), ref(20, "g"), ref(30, "g"))
            for i in range(20_001)
        ]
        plan = plan_document((0, 0, 3, 1), old, new, too_many, [])
        self.assert_rejected(plan, old, new, "20000", prefix="many")

    def test_duplicate_block_ids_are_rejected(self):
        old, new = base_views()
        old_blocks = [
            block("same", (0, 0, 1, 1),
                  ref(10, "g"), ref(20, "g"), ref(30, "g")),
            block("same", (1, 0, 2, 1),
                  ref(10, "g"), ref(20, "g"), ref(30, "g")),
        ]
        new_blocks = [
            block("n", (0, 0, 2, 1),
                  ref(10, "g"), ref(20, "g"), ref(30, "g"))
        ]
        plan = plan_document((0, 0, 2, 1), old, new, old_blocks, new_blocks)
        self.assert_rejected(plan, old, new, "repeats block_id", prefix="dup")

    def test_output_is_never_overwritten_and_never_an_input(self):
        old, new = base_views()
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        paths = self.write_inputs(plan, old, new, "protect")
        target = write_json(self.root / "protect-package.json", {"sentinel": 1})
        result = run_cli(
            "earthwork", "--old", paths["old"], "--new", paths["new"],
            "--plan", paths["plan"], "--output", target, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(read_json(target), {"sentinel": 1})

        result = run_cli(
            "earthwork", "--old", paths["old"], "--new", paths["new"],
            "--plan", paths["plan"], "--output", paths["old"], check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("input", result.stderr)
        self.assertEqual(read_json(paths["old"])["fingerprint"],
                         old["fingerprint"])


# ---------------------------------------------------------------------------
# Determinism


class DeterminismTests(EarthworkCase):
    def test_permuting_declarations_and_keys_keeps_package_bytes(self):
        old = view_document(
            {(10, "p"): ["0", "0", "0"], (20, "p"): ["1", "0", "0"],
             (30, "p"): ["0", "1", "0"], (40, "p"): ["2", "0", "0"],
             (50, "p"): ["1", "1", "0"]}, "old")
        new = view_document(
            {(10, "q"): ["0", "0", "1"], (30, "q"): ["0", "1", "1"],
             (40, "q"): ["2", "0", "1"], (60, "q"): ["2", "1", "1"]}, "new")
        old_blocks = [
            block("o1", (0, 0, 1, 1),
                  ref(10, "p"), ref(20, "p"), ref(30, "p")),
            block("o2", (1, 0, 2, 1),
                  ref(20, "p"), ref(40, "p"), ref(50, "p")),
        ]
        new_blocks = [
            block("n1", (0, 0, 2, 1),
                  ref(10, "q"), ref(40, "q"), ref(30, "q")),
            block("n2", (2, 0, 3, 1),
                  ref(40, "q"), ref(99, "q"), ref(60, "q")),
        ]
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        _, _result, first = self.earthwork(plan, old, new, prefix="det-a")
        original = first.read_bytes()

        rng = random.Random(196)
        shuffled_plan = json.loads(json.dumps(plan))
        rng.shuffle(shuffled_plan["old"]["blocks"])
        rng.shuffle(shuffled_plan["new"]["blocks"])
        shuffled_old = json.loads(json.dumps(old))
        shuffled_new = json.loads(json.dumps(new))
        rng.shuffle(shuffled_old["points"])
        rng.shuffle(shuffled_new["points"])
        reversed_plan = _reverse_keys(shuffled_plan)
        _, _result, second = self.earthwork(
            reversed_plan, shuffled_old, shuffled_new, prefix="det-b"
        )
        self.assertEqual(original, second.read_bytes())
        first_package = json.loads(original)
        second_package = json.loads(second.read_bytes())
        self.assertEqual(
            first_package["package_digest"], second_package["package_digest"]
        )
        self.assertTrue(
            first_package["package_digest"].startswith(
                "camera-tracks-earthwork-v1:"
            )
        )


def _reverse_keys(value):
    if isinstance(value, dict):
        return {key: _reverse_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


class AtomicPublishTests(EarthworkCase):
    """A failed write never leaves anything at the target path."""

    def test_failed_stream_leaves_no_target_and_no_temporary(self):
        import io as std_io
        from camera_tracks import io as ct_io

        def explode(_stream):
            _stream.write(b'{"schema_version":1,"kind":"camera-tracks-earthwork"')
            raise RuntimeError("injected interruption mid-publish")

        target = self.root / "partial.json"
        with self.assertRaises(RuntimeError):
            ct_io.publish_new_stream(target, explode)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".publish-*")), [])

    def test_published_package_is_one_complete_json_document(self):
        old, new = base_views()
        old_blocks, new_blocks = base_blocks()
        plan = plan_document((0, 0, 3, 1), old, new, old_blocks, new_blocks)
        package, _result, output = self.earthwork(plan, old, new, prefix="atomic")
        raw = output.read_bytes()
        self.assertTrue(raw.endswith(b"}\n"))
        parsed = json.loads(raw)
        self.assertEqual(parsed, package)
        self.assertEqual(parsed["slice_count"], len(parsed["slices"]))


class DeliveryChainTests(EarthworkCase):
    """One end-to-end settlement over views exported by the delivery chain."""

    def deliver_view(self, side):
        build = self.root / f"{side}-build.json"
        build.write_bytes(
            (ROOT / "fixtures" / f"earthwork-{side}-build.json").read_bytes()
        )
        upstream = self.root / f"{side}-upstream.json"
        full = self.root / f"{side}-full.json"
        downstream = self.root / f"{side}-downstream.json"
        view = self.root / f"{side}-view.json"
        run_cli("build", "--input", build, "--store", upstream)
        run_cli("export", "--store", upstream, "--full", "--output", full)
        run_cli("apply", "--store", downstream, "--package", full)
        run_cli("view", "--store", downstream, "--output", view)
        return view

    def test_settlement_over_delivered_views(self):
        old_view = self.deliver_view("old")
        new_view = self.deliver_view("new")
        plan = write_json(
            self.root / "plan.json",
            read_json(ROOT / "fixtures" / "earthwork-plan.json"),
        )
        output = self.root / "package.json"
        result = run_cli(
            "earthwork", "--old", old_view, "--new", new_view,
            "--plan", plan, "--output", output,
        )
        summary = json.loads(result.stdout)
        self.assertEqual(
            (summary["slices"], summary["settled_area"],
             summary["unsettled_area"], summary["settled_cut"],
             summary["settled_fill"], summary["status"]),
            (1, "1", "2", "1/8", "1/8", "partial"),
        )
        package = read_json(output)
        self.assertEqual(package["slices"][0]["range"], [1, 0, 2, 1])
        # Both endpoints name the views the delivery chain actually emitted.
        old_document = read_json(old_view)
        new_document = read_json(new_view)
        self.assertEqual(package["old"]["fingerprint"],
                         old_document["fingerprint"])
        self.assertEqual(package["new"]["fingerprint"],
                         new_document["fingerprint"])
        self.assertEqual(package["old"]["dataset_id"], "earthwork-old")
        self.assertEqual(package["new"]["dataset_id"], "earthwork-new")

        # The original single-dataset entry point still answers unchanged
        # after this settlement, and consuming an existing point table works.
        legacy = run_cli(
            "--source", ROOT / "fixtures" / "input.json",
            "--output", self.root / "legacy.json",
        )
        self.assertEqual(
            json.loads(legacy.stdout),
            {"cameras": 2, "reconstructed": 1, "tracks": 1,
             "tracks_with_nonpositive_depth": 0, "unobservable": 0},
        )
        queried = run_cli(
            "view", "--store", self.root / "old-downstream.json",
            "--sequence", 20, "--track", "ground",
        )
        self.assertEqual(json.loads(queried.stdout)["xyz"], ["2", "0", "0"])


class PropertyTests(EarthworkCase):
    """Random exact partitions cross-checked against dense midpoint sampling."""

    @staticmethod
    def partition(rng, width, height):
        rects = []
        stack = [(0, 0, width, height)]
        while stack:
            left, bottom, right, top = stack.pop()
            area = (right - left) * (top - bottom)
            if area == 1 or (area <= 3 and rng.random() < 0.6):
                rects.append((left, bottom, right, top))
                continue
            if right - left >= top - bottom and right - left > 1:
                middle = rng.randint(left + 1, right - 1)
                stack.append((left, bottom, middle, top))
                stack.append((middle, bottom, right, top))
            elif top - bottom > 1:
                middle = rng.randint(bottom + 1, top - 1)
                stack.append((left, bottom, right, middle))
                stack.append((left, middle, right, top))
            else:
                rects.append((left, bottom, right, top))
        return rects

    def test_random_partitions_match_dense_sampling(self):
        for seed in range(6):
            rng = random.Random(seed)
            width = rng.randint(5, 9)
            height = rng.randint(5, 9)
            old_rects = self.partition(rng, width, height)
            new_rects = self.partition(rng, width, height)
            old_plane = tuple(
                Fraction(rng.randint(-3, 3), rng.randint(1, 2)) for _ in range(3)
            )
            old_points, new_points = {}, {}
            old_blocks, new_blocks, new_planes = [], [], {}

            def anchor(points, x, y, z, tag):
                sequence = x * 1000 + y
                points[(sequence, tag)] = [
                    str(Fraction(x)), str(Fraction(y)), str(z)
                ]
                return {"sequence": sequence, "track_id": tag}

            for index, (left, bottom, right, top) in enumerate(old_rects):
                tag = f"old{index}"
                heights = [
                    old_plane[0] * x + old_plane[1] * y + old_plane[2]
                    for x, y in ((left, bottom), (right, bottom), (left, top))
                ]
                old_blocks.append(
                    block(
                        f"o{index}", (left, bottom, right, top),
                        anchor(old_points, left, bottom, heights[0], tag),
                        anchor(old_points, right, bottom, heights[1], tag),
                        anchor(old_points, left, top, heights[2], tag),
                    )
                )
            for index, (left, bottom, right, top) in enumerate(new_rects):
                tag = f"new{index}"
                plane = tuple(
                    Fraction(rng.randint(-2, 2), rng.randint(1, 2))
                    for _ in range(3)
                )
                new_planes[(left, bottom, right, top)] = plane
                heights = [
                    plane[0] * x + plane[1] * y + plane[2]
                    for x, y in ((left, bottom), (right, bottom), (left, top))
                ]
                new_blocks.append(
                    block(
                        f"n{index}", (left, bottom, right, top),
                        anchor(new_points, left, bottom, heights[0], tag),
                        anchor(new_points, right, bottom, heights[1], tag),
                        anchor(new_points, left, top, heights[2], tag),
                    )
                )
            old = view_document(old_points, "old-property")
            new = view_document(new_points, "new-property")
            plan = plan_document(
                (0, 0, width, height), old, new, old_blocks, new_blocks,
                zone_id=f"property-{seed}",
            )
            package, _result, _output = self.earthwork(
                plan, old, new, prefix=f"property-{seed}"
            )
            self.assertEqual(int(package["settled_area"]), width * height)
            self.assertEqual(int(package["unsettled_area"]), 0)
            self.assertEqual(package["status"], "complete")

            def old_height(x, y):
                return old_plane[0] * x + old_plane[1] * y + old_plane[2]

            def new_height(x, y):
                for (left, bottom, right, top), plane in new_planes.items():
                    if left <= x < right and bottom <= y < top:
                        return plane[0] * x + plane[1] * y + plane[2]
                raise AssertionError(f"point ({x}, {y}) is outside the new blocks")

            steps = 180
            dx = Fraction(width, steps)
            dy = Fraction(height, steps)
            cut = fill = Fraction(0)
            for i in range(steps):
                x = (i + Fraction(1, 2)) * dx
                for j in range(steps):
                    y = (j + Fraction(1, 2)) * dy
                    value = (new_height(x, y) - old_height(x, y)) * dx * dy
                    if value > 0:
                        fill += value
                    else:
                        cut -= value
            got_cut = Fraction(package["settled_cut"])
            got_fill = Fraction(package["settled_fill"])
            scale = max(abs(got_cut), abs(got_fill), Fraction(1))
            tolerance = scale / 100 + 3
            self.assertLess(abs(got_cut - cut), tolerance, f"seed {seed}")
            self.assertLess(abs(got_fill - fill), tolerance, f"seed {seed}")
            covered = 0
            for entry in package["slices"]:
                left, bottom, right, top = entry["range"]
                covered += (right - left) * (top - bottom)
                self.assertGreaterEqual(Fraction(entry["cut"]), 0)
                self.assertGreaterEqual(Fraction(entry["fill"]), 0)
            self.assertEqual(covered, width * height)


# ---------------------------------------------------------------------------
# 317 x 317 strip sample: the slice limit


STRIPS = 317


def strips_inputs(removed=()):
    """Old vertical strips over new horizontal strips; 100,489 full slices."""
    removed = set(removed)
    old_points = {}
    for i in range(STRIPS + 1):
        for y in (0, STRIPS):
            if (i, y) in removed:
                continue
            old_points[(i, f"g{i}_{y}")] = [str(i), str(y), "0"]
    old_blocks = [
        block(
            f"v{i}", (i, 0, i + 1, STRIPS),
            ref(i, f"g{i}_0"), ref(i + 1, f"g{i+1}_0"),
            ref(i, f"g{i}_{STRIPS}"),
        )
        for i in range(STRIPS)
    ]
    new_points = {}
    for j in range(STRIPS + 1):
        for x in (0, STRIPS):
            new_points[(j, f"h{j}_{x}")] = [str(x), str(j), "1"]
    new_blocks = [
        block(
            f"h{j}", (0, j, STRIPS, j + 1),
            ref(j, f"h{j}_0"), ref(j, f"h{j}_{STRIPS}"),
            ref(j + 1, f"h{j+1}_0"),
        )
        for j in range(STRIPS)
    ]
    old = view_document(old_points, "strips-old")
    new = view_document(new_points, "strips-new")
    plan = plan_document(
        (0, 0, STRIPS, STRIPS), old, new, old_blocks, new_blocks,
        zone_id="strips",
    )
    return old, new, plan


class StripCapacityTests(EarthworkCase):
    def measured(self, plan, old, new, prefix):
        paths = self.write_inputs(plan, old, new, prefix)
        output = self.root / f"{prefix}-package.json"
        started = time.perf_counter()
        result, usage = measure_cli(
            "earthwork", "--old", paths["old"], "--new", paths["new"],
            "--plan", paths["plan"], "--output", output,
        )
        elapsed = time.perf_counter() - started
        return result, output, usage[0], usage[1], elapsed

    def assert_refused(self, plan, old, new, prefix):
        result, output, cpu, rss, _elapsed = self.measured(plan, old, new, prefix)
        self.assertEqual(result.returncode, 2)
        self.assertIn("100000", result.stderr)
        self.assertFalse(output.exists(), "refusal left a package at the target")
        self.assertEqual(list(self.root.glob(".publish-*")), [])
        self.assertEqual(result.stdout.strip(), "")
        self.assertLess(cpu, 20)
        self.assertLess(rss, 192 * 2**20)
        return cpu, rss

    def test_full_grid_has_100489_slices_and_is_refused(self):
        old, new, plan = strips_inputs()
        self.assert_refused(plan, old, new, "cap-full")

    def test_one_unused_anchor_gap_still_exceeds_the_limit(self):
        # (0, 0) is the lower-left anchor of strip v0 only.
        old, new, plan = strips_inputs(removed={(0, 0)})
        self.assert_refused(plan, old, new, "cap-one")

    def test_two_strip_gaps_publish_99855_slices(self):
        old, new, plan = strips_inputs(removed={(0, 0), (STRIPS, 0)})
        result, output, cpu, rss, elapsed = self.measured(
            plan, old, new, "cap-two"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(cpu, 20, f"earthwork took {cpu:.2f}s CPU")
        self.assertLess(elapsed, 60, f"earthwork took {elapsed:.1f}s wall")
        self.assertLess(rss, 192 * 2**20, f"peak RSS {rss / 2**20:.0f} MiB")
        package = read_json(output)
        self.assertEqual(len(package["slices"]), 315 * STRIPS)
        self.assertEqual(package["slice_count"], 315 * STRIPS)
        self.assertEqual(int(package["settled_area"]), 315 * STRIPS)
        self.assertEqual(int(package["unsettled_area"]),
                         STRIPS * STRIPS - 315 * STRIPS)
        self.assertEqual(int(package["zone_area"]), STRIPS * STRIPS)
        self.assertEqual(Fraction(package["settled_fill"]), 315 * STRIPS)
        self.assertEqual(Fraction(package["settled_cut"]), 0)
        self.assertEqual(package["status"], "partial")
        self.assertEqual(len(package["missing_blocks"]), 2)
        self.assertEqual(
            [(entry["layer"], entry["block_id"]) for entry in package["missing_blocks"]],
            [("old", "v0"), ("old", f"v{STRIPS - 1}")],
        )
        covered = 0
        seen = set()
        for entry in package["slices"]:
            left, bottom, right, top = entry["range"]
            self.assertTrue(0 <= left < right <= STRIPS)
            self.assertTrue(0 <= bottom < top <= STRIPS)
            self.assertNotEqual(entry["old_block"], "v0")
            self.assertNotEqual(entry["old_block"], f"v{STRIPS - 1}")
            covered += (right - left) * (top - bottom)
            seen.add((left, bottom, right, top))
        self.assertEqual(covered, 315 * STRIPS)
        self.assertEqual(len(seen), len(package["slices"]))


# ---------------------------------------------------------------------------
# 140 x 140 batch sample


BLOCKS = 140
L = 1_000_000


def batch_inputs(shuffle_seed=None):
    def grid_points(offset, height):
        points = {}
        for i in range(BLOCKS + 1):
            for j in range(BLOCKS + 1):
                points[(i * 1000 + j, f"p{i}_{j}")] = [
                    str(offset + i * 2 * L), str(offset + j * 2 * L), height,
                ]
        return points

    def blocks(offset, tag):
        result = []
        for i in range(BLOCKS):
            for j in range(BLOCKS):
                result.append(
                    block(
                        f"{tag}{i}_{j}",
                        (offset + i * 2 * L, offset + j * 2 * L,
                         offset + (i + 1) * 2 * L, offset + (j + 1) * 2 * L),
                        ref(i * 1000 + j, f"p{i}_{j}"),
                        ref((i + 1) * 1000 + j, f"p{i+1}_{j}"),
                        ref(i * 1000 + j + 1, f"p{i}_{j+1}"),
                    )
                )
        return result

    old = view_document(grid_points(0, "0"), "batch-old")
    new = view_document(grid_points(L, "2"), "batch-new")
    plan = plan_document(
        (0, 0, 281 * L, 281 * L), old, new, blocks(0, "o"), blocks(L, "n"),
        zone_id="batch",
    )
    if shuffle_seed is not None:
        rng = random.Random(shuffle_seed)
        rng.shuffle(plan["old"]["blocks"])
        rng.shuffle(plan["new"]["blocks"])
        rng.shuffle(old["points"])
        rng.shuffle(new["points"])
    return old, new, plan


class BatchCapacityTests(EarthworkCase):
    def run_batch(self, prefix, shuffle_seed=None):
        old, new, plan = batch_inputs(shuffle_seed)
        paths = self.write_inputs(plan, old, new, prefix)
        output = self.root / f"{prefix}-package.json"
        started = time.perf_counter()
        result, usage = measure_cli(
            "earthwork", "--old", paths["old"], "--new", paths["new"],
            "--plan", paths["plan"], "--output", output,
        )
        elapsed = time.perf_counter() - started
        cpu, rss = usage
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(cpu, 20, f"earthwork took {cpu:.2f}s CPU")
        self.assertLess(elapsed, 60, f"earthwork took {elapsed:.1f}s wall")
        self.assertLess(rss, 192 * 2**20, f"peak RSS {rss / 2**20:.0f} MiB")
        return read_json(output), output.read_bytes(), plan, cpu, rss

    def verify(self, package, plan):
        axis = 2 * BLOCKS - 1
        expected = axis * axis
        self.assertEqual(len(package["slices"]), expected)
        self.assertEqual(package["slice_count"], expected)
        self.assertEqual(int(package["settled_area"]), expected * L * L)
        self.assertEqual(int(package["unsettled_area"]),
                         (281**2 - expected) * L * L)
        self.assertEqual(int(package["zone_area"]), 281**2 * L * L)
        self.assertEqual(Fraction(package["settled_fill"]), 2 * expected * L * L)
        self.assertEqual(Fraction(package["settled_cut"]), 0)
        self.assertEqual(package["status"], "partial")
        self.assertEqual(package["missing_blocks"], [])

        old_area = {b["block_id"]: b["area"] for b in plan["old"]["blocks"]}
        new_area = {b["block_id"]: b["area"] for b in plan["new"]["blocks"]}
        expected_keys = set()
        for i in range(BLOCKS):
            for j in range(BLOCKS):
                for k in (i - 1, i):
                    for l in (j - 1, j):
                        if not (0 <= k < BLOCKS and 0 <= l < BLOCKS):
                            continue
                        a = old_area[f"o{i}_{j}"]
                        b = new_area[f"n{k}_{l}"]
                        left, bottom = max(a[0], b[0]), max(a[1], b[1])
                        right, top = min(a[2], b[2]), min(a[3], b[3])
                        if left < right and bottom < top:
                            expected_keys.add(
                                (f"o{i}_{j}", f"n{k}_{l}", left, bottom, right, top)
                            )
        self.assertEqual(len(expected_keys), expected)
        covered = set()
        for entry in package["slices"]:
            key = (entry["old_block"], entry["new_block"], *entry["range"])
            self.assertIn(key, expected_keys)
            self.assertNotIn(key, covered)
            covered.add(key)
            # Constant 2 m new-minus-old everywhere: four exact corner values.
            self.assertEqual(set(entry["corner_differences"].values()), {"2"})
            left, bottom, right, top = entry["range"]
            self.assertEqual(
                (right - left) * (top - bottom), L * L,
                f"slice {key} is not one L x L cell",
            )
        self.assertEqual(covered, expected_keys)

    def test_batch_package_and_shuffled_bytes(self):
        package, payload, plan, cpu, rss = self.run_batch("batch")
        self.verify(package, plan)
        shuffled, shuffled_payload, shuffled_plan, cpu2, rss2 = \
            self.run_batch("batch-shuffled", shuffle_seed=196)
        self.verify(shuffled, shuffled_plan)
        self.assertEqual(payload, shuffled_payload)


if __name__ == "__main__":
    unittest.main()
