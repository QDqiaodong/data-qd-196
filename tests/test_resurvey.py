"""Resurvey work-order (补测工单) subcommand tests."""

import json
from pathlib import Path
import random
import resource
import sys
import tempfile
import time
import unittest

from fractions import Fraction

from helpers import SRC, build_document, change_document, read_json, replacement, run_cli, write_json

sys.path.insert(0, str(SRC))
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402


def view_document(points, dataset_id="survey-196", version=0):
    """A view export equivalent, straight from the canonical point table."""
    table = {point_key(s, t): list(xyz) for (s, t), xyz in points.items()}
    return {
        "schema_version": 1,
        "kind": "camera-tracks-view",
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


def plan_document(view, columns, rows, required=2, pass_rectangles=(),
                  forbidden_rectangles=(), zone_id="zone-7",
                  x0="0", y0="0", cell_size="1", z_min="0", z_max="1"):
    return {
        "schema_version": 1,
        "kind": "camera-tracks-resurvey-plan",
        "zone_id": zone_id,
        "view": endpoint(view),
        "grid": {
            "x0": x0,
            "y0": y0,
            "cell_size": cell_size,
            "columns": columns,
            "rows": rows,
        },
        "z_min": z_min,
        "z_max": z_max,
        "required_sequences": required,
        "pass_rectangles": [list(rect) for rect in pass_rectangles],
        "forbidden_rectangles": [list(rect) for rect in forbidden_rectangles],
    }


def reorder_keys(value):
    """Same JSON value with every object's keys in reverse order."""
    if isinstance(value, dict):
        return {key: reorder_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reorder_keys(item) for item in value]
    return value


def rect_cells(rect):
    left, bottom, right, top = rect
    return {
        (column, row)
        for column in range(left, right)
        for row in range(bottom, top)
    }


def order_cells(order):
    """Cells covered by an order's rectangles; fails if they overlap."""
    cells = set()
    for rect in order["rectangles"]:
        fresh = rect_cells(rect)
        self_disjoint = cells.isdisjoint(fresh)
        if not self_disjoint:
            raise AssertionError(f"overlapping rectangles in order {order}")
        cells |= fresh
    return cells


def union_area(rects):
    """Independent sweep-line union area of half-open integer rectangles."""
    events = []
    for left, bottom, right, top in rects:
        events.append((left, 1, bottom, top))
        events.append((right, -1, bottom, top))
    events.sort()
    area = 0
    active = []
    previous_x = 0
    for x, delta, bottom, top in events:
        if active and x > previous_x:
            active.sort()
            covered = 0
            span_bottom, span_top = active[0]
            for b, t in active[1:]:
                if b > span_top:
                    covered += span_top - span_bottom
                    span_bottom, span_top = b, t
                elif t > span_top:
                    span_top = t
            covered += span_top - span_bottom
            area += (x - previous_x) * covered
        previous_x = x
        if delta == 1:
            active.append((bottom, top))
        else:
            active.remove((bottom, top))
    return area


class ResurveyCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def resurvey(self, plan, view, check=True, name="zone"):
        plan_path = write_json(self.root / f"{name}.plan.json", plan)
        view_path = write_json(self.root / f"{name}.view.json", view)
        output = self.root / f"{name}.report.json"
        result = run_cli(
            "resurvey", "--view", view_path, "--plan", plan_path,
            "--output", output, check=check,
        )
        report = read_json(output) if output.exists() else None
        return result, report, output

    def reject(self, plan, view, needle, name="bad"):
        result, report, output = self.resurvey(plan, view, check=False, name=name)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(needle, result.stderr)
        self.assertIsNone(report)
        self.assertFalse(output.exists())
        return result


# The 5x2 scenario from the resurvey contract: cell (2,1) is forbidden,
# cell (2,0) is an exempt passage, and two batches are required per cell.
SCENARIO_POINTS = {
    (10, "a"): ["1/2", "3/2", "1/2"],  # cell (0,1), batch 10
    (20, "b"): ["1/2", "3/2", "1/2"],  # cell (0,1), batch 20 -> accepted
    (30, "c"): ["9/2", "3/2", "1/2"],  # cell (4,1), batch 30 ...
    (30, "d"): ["9/2", "3/2", "1/2"],  # ... two tracks, still one batch
    (40, "e"): ["9/2", "3/2", "1"],    # batch 40 but z == z_max: not valid
    (50, "f"): ["1/2", "1/2", "1/2"],  # cell (0,0), batch 50
    (60, "g"): ["-1/2", "0", "0"],     # batch 60 but x outside the grid
}
SCENARIO_PENDING = {(0, 0), (1, 0), (3, 0), (4, 0), (1, 1), (3, 1), (4, 1)}


class GridScenarioTests(ResurveyCase):
    def scenario(self, forbidden_rectangles):
        view = view_document(SCENARIO_POINTS)
        plan = plan_document(
            view, 5, 2, required=2,
            pass_rectangles=[[2, 0, 3, 1]],
            forbidden_rectangles=forbidden_rectangles,
        )
        return view, plan

    def test_mixed_classes_form_one_order(self):
        view, plan = self.scenario([[2, 1, 3, 2]])
        result, report, _ = self.resurvey(plan, view)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["orders"], 1)
        self.assertEqual(
            report["cells"],
            {"accepted": "1", "pending": "7", "passage": "1",
             "forbidden": "1", "total": "10"},
        )
        self.assertEqual(len(report["orders"]), 1)
        order = report["orders"][0]
        self.assertEqual(order["identity"], [0, 0])
        self.assertEqual(order["pending_cells"], "7")
        self.assertEqual(order["bounding_box"], [0, 0, 5, 2])
        # The rectangles cover exactly the pending cells: no passage cell
        # (2,0), no forbidden cell (2,1), no accepted cell (0,1).
        self.assertEqual(order_cells(order), SCENARIO_PENDING)
        self.assertNotIn((2, 0), order_cells(order))
        self.assertNotIn((2, 1), order_cells(order))
        self.assertNotIn((0, 1), order_cells(order))
        self.assertEqual(
            sum(
                (r[2] - r[0]) * (r[3] - r[1]) for r in order["rectangles"]
            ),
            7,
        )

    def test_forbidden_over_passage_splits_into_two_orders(self):
        # Same point table and same passage declaration; only an added
        # forbidden rectangle over the passage cell.  Pending stays 7 but
        # the corridor is cut: left 3 cells and right 4 cells.
        view, plan = self.scenario([[2, 1, 3, 2], [2, 0, 3, 1]])
        result, report, _ = self.resurvey(plan, view)
        self.assertEqual(
            report["cells"],
            {"accepted": "1", "pending": "7", "passage": "0",
             "forbidden": "2", "total": "10"},
        )
        self.assertEqual(len(report["orders"]), 2)
        left, right = report["orders"]
        self.assertEqual(left["identity"], [0, 0])
        self.assertEqual(left["pending_cells"], "3")
        self.assertEqual(order_cells(left), {(0, 0), (1, 0), (1, 1)})
        self.assertEqual(right["identity"], [3, 0])
        self.assertEqual(right["pending_cells"], "4")
        self.assertEqual(order_cells(right), {(3, 0), (4, 0), (3, 1), (4, 1)})
        total = sum(int(order["pending_cells"]) for order in report["orders"])
        self.assertEqual(total, 7)


class ConnectivityTests(ResurveyCase):
    def test_corner_contact_does_not_merge_orders(self):
        # (0,1) and (1,0) are accepted; the two pending cells (0,0) and
        # (1,1) touch only at a corner and must stay two orders.
        view = view_document({
            (10, "a"): ["1/2", "3/2", "1/2"],
            (20, "b"): ["3/2", "1/2", "1/2"],
        })
        plan = plan_document(view, 2, 2, required=1)
        _, report, _ = self.resurvey(plan, view)
        self.assertEqual(
            report["cells"],
            {"accepted": "2", "pending": "2", "passage": "0",
             "forbidden": "0", "total": "4"},
        )
        self.assertEqual(len(report["orders"]), 2)
        first, second = report["orders"]
        self.assertEqual(first["identity"], [0, 0])
        self.assertEqual(order_cells(first), {(0, 0)})
        self.assertEqual(second["identity"], [1, 1])
        self.assertEqual(order_cells(second), {(1, 1)})

    def test_passage_only_component_emits_no_order(self):
        # The passage cell (1,0) is walled in by accepted cells; a component
        # without any pending cell produces no work order.
        view = view_document({
            (10, "a"): ["1/2", "1/2", "1/2"],
            (20, "b"): ["5/2", "1/2", "1/2"],
        })
        plan = plan_document(
            view, 3, 1, required=1, pass_rectangles=[[1, 0, 2, 1]]
        )
        result, report, _ = self.resurvey(plan, view)
        self.assertEqual(
            report["cells"],
            {"accepted": "2", "pending": "0", "passage": "1",
             "forbidden": "0", "total": "3"},
        )
        self.assertEqual(report["orders"], [])
        self.assertEqual(json.loads(result.stdout)["orders"], 0)

    def test_zero_orders_is_a_normal_result(self):
        view = view_document({(10, "a"): ["1/2", "1/2", "1/2"]})
        plan = plan_document(view, 1, 1, required=1)
        result, report, _ = self.resurvey(plan, view)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["cells"]["accepted"], "1")
        self.assertEqual(report["orders"], [])

    def test_passage_glues_pending_cells_into_one_order(self):
        # Two pending cells joined only through an exempt passage corridor.
        view = view_document({})
        plan = plan_document(
            view, 3, 1, required=1,
            pass_rectangles=[[1, 0, 2, 1]],
        )
        _, report, _ = self.resurvey(plan, view)
        self.assertEqual(report["cells"]["pending"], "2")
        self.assertEqual(report["cells"]["passage"], "1")
        self.assertEqual(len(report["orders"]), 1)
        order = report["orders"][0]
        self.assertEqual(order["pending_cells"], "2")
        self.assertEqual(order_cells(order), {(0, 0), (2, 0)})


class PlanValidationTests(ResurveyCase):
    def simple(self, **overrides):
        view = view_document({(10, "a"): ["1/2", "1/2", "1/2"]})
        plan = plan_document(view, 2, 2, required=1)
        plan.update(overrides)
        return plan, view

    def test_same_fingerprint_other_version_is_rejected(self):
        plan, view = self.simple()
        stale = dict(view)
        stale["version"] = 1
        plan["view"] = endpoint(stale)
        self.reject(plan, view, "does not match the planned")

    def test_tampered_point_with_stale_fingerprint_is_rejected(self):
        plan, view = self.simple()
        tampered = json.loads(json.dumps(view))
        tampered["points"][0]["xyz"][0] = "99"
        self.reject(plan, tampered, "does not match its point table")

    def test_planned_fingerprint_mismatch_is_rejected(self):
        plan, view = self.simple()
        plan["view"]["fingerprint"] = "0" * 64
        self.reject(plan, view, "does not match the planned")

    def test_rectangle_outside_grid_is_rejected(self):
        for rect in ([0, 0, 3, 1], [0, 0, 1, 3], [1, 1, 1, 2], [0, 0, 0, 1]):
            plan, view = self.simple(forbidden_rectangles=[rect])
            self.reject(plan, view, "inside the grid", name=f"rect{rect}")
            plan, view = self.simple(pass_rectangles=[rect])
            self.reject(plan, view, "inside the grid", name=f"pass{rect}")

    def test_rectangle_limit_is_enforced(self):
        plan, view = self.simple(
            forbidden_rectangles=[[0, 0, 1, 1]] * 25_001,
        )
        self.reject(plan, view, "array of size 0..25000", name="toomany")
        plan, view = self.simple(
            pass_rectangles=[[0, 0, 1, 1]] * 25_000,
            forbidden_rectangles=[[0, 0, 1, 1]],
        )
        self.reject(plan, view, "at most 25000", name="toomany2")

    def test_grid_and_band_constraints(self):
        plan, view = self.simple()
        plan["grid"]["cell_size"] = "0"
        self.reject(plan, view, "cell_size must be positive", name="size")
        plan, view = self.simple(z_min="1", z_max="1")
        self.reject(plan, view, "z_min must be smaller", name="band")
        plan, view = self.simple()
        plan["grid"]["columns"] = 0
        self.reject(plan, view, "columns", name="cols")
        plan, view = self.simple()
        plan["required_sequences"] = 0
        self.reject(plan, view, "required_sequences", name="req0")
        plan, view = self.simple()
        plan["required_sequences"] = 101
        self.reject(plan, view, "required_sequences", name="req101")

    def test_existing_report_is_never_overwritten(self):
        plan, view = self.simple()
        output = self.root / "taken.report.json"
        output.write_text("previous report", encoding="utf-8")
        for broken in (False, True):
            candidate = json.loads(json.dumps(plan)) if broken else plan
            if broken:
                candidate["forbidden_rectangles"] = [[0, 0, 3, 1]]
            result = run_cli(
                "resurvey", "--view", write_json(self.root / "v.json", view),
                "--plan", write_json(self.root / "p.json", candidate),
                "--output", output, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous report")

    def test_output_must_not_point_at_an_input(self):
        plan, view = self.simple()
        plan_path = write_json(self.root / "plan.json", plan)
        result = run_cli(
            "resurvey", "--view", write_json(self.root / "v.json", view),
            "--plan", plan_path, "--output", plan_path, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("must differ from the input", result.stderr)
        self.assertEqual(read_json(plan_path), plan)

    def test_permuted_inputs_keep_report_bytes(self):
        points = {
            (10, "a"): ["1/2", "3/2", "1/2"],
            (20, "b"): ["1/2", "3/2", "1/2"],
            (30, "c"): ["9/2", "3/2", "1/2"],
            (40, "e"): ["9/2", "3/2", "1"],
            (50, "f"): ["1/2", "1/2", "1/2"],
        }
        view = view_document(points)
        plan = plan_document(
            view, 5, 2, required=2,
            pass_rectangles=[[2, 0, 3, 1], [4, 0, 5, 1]],
            forbidden_rectangles=[[2, 1, 3, 2], [4, 1, 5, 2], [0, 0, 1, 1]],
        )
        _, _, output = self.resurvey(plan, view, name="ordered")
        shuffled_view = json.loads(json.dumps(view))
        shuffled_view["points"] = list(reversed(shuffled_view["points"]))
        shuffled_plan = json.loads(json.dumps(plan))
        shuffled_plan["pass_rectangles"] = list(reversed(plan["pass_rectangles"]))
        shuffled_plan["forbidden_rectangles"] = list(
            reversed(plan["forbidden_rectangles"])
        )
        _, _, shuffled_output = self.resurvey(
            reorder_keys(shuffled_plan), reorder_keys(shuffled_view),
            name="shuffled",
        )
        self.assertEqual(output.read_bytes(), shuffled_output.read_bytes())


class ResurveyPipelineTests(ResurveyCase):
    """A real view export from the delivery chain, end to end."""

    def build_view(self, name, document, corrections=()):
        upstream = self.root / f"{name}.upstream.json"
        downstream = self.root / f"{name}.downstream.json"
        run_cli("build", "--input", write_json(self.root / f"{name}.build.json", document),
                "--store", upstream)
        run_cli("export", "--store", upstream, "--full", "--output",
                self.root / f"{name}.full.json")
        run_cli("apply", "--store", downstream, "--package", self.root / f"{name}.full.json")
        for change_id, request in corrections:
            run_cli("correct", "--store", upstream, "--request",
                    write_json(self.root / f"{change_id}.json", request))
            run_cli("export", "--store", upstream, "--base-version",
                    request["base_version"], "--output", self.root / f"{change_id}.json")
            run_cli("apply", "--store", downstream, "--package", self.root / f"{change_id}.json")
        view_path = self.root / f"{name}.view.json"
        run_cli("view", "--store", downstream, "--output", view_path)
        return read_json(view_path)

    def test_resurvey_over_exported_view_and_stale_version_rejection(self):
        document = build_document(dataset_id="survey-196", sequences=(10, 20, 30))
        # A correction that touches no sequence leaves the view identical, so
        # version 1 has the same fingerprint as version 0.
        correction = change_document(
            "noop", 0, [replacement(40, 50, last="-1")], dataset_id="survey-196"
        )
        view_v1 = self.build_view("fresh", document, [("noop", correction)])
        view_v0 = self.build_view("stale", document)
        self.assertEqual(view_v0["fingerprint"], view_v1["fingerprint"])
        self.assertNotEqual(view_v0["version"], view_v1["version"])

        # The three delivered points [2,1,4] all land in cell (2,1); with
        # three required batches that cell is accepted, the rest is pending.
        plan = plan_document(
            view_v1, 4, 4, required=3, z_min="0", z_max="10",
        )
        result, report, _ = self.resurvey(plan, view_v0, check=False, name="stale")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("does not match the planned", result.stderr)

        result, report, _ = self.resurvey(plan, view_v1, name="fresh")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            report["cells"],
            {"accepted": "1", "pending": "15", "passage": "0",
             "forbidden": "0", "total": "16"},
        )
        self.assertEqual(len(report["orders"]), 1)
        order = report["orders"][0]
        self.assertEqual(order["identity"], [0, 0])
        self.assertEqual(order["pending_cells"], "15")
        self.assertEqual(order["bounding_box"], [0, 0, 4, 4])
        expected = {
            (column, row)
            for column in range(4)
            for row in range(4)
            if (column, row) != (2, 1)
        }
        self.assertEqual(order_cells(order), expected)


class RandomizedBruteForceTests(ResurveyCase):
    """Random small grids checked against a per-cell reference model."""

    def reference(self, plan, view):
        grid = plan["grid"]
        columns, rows = grid["columns"], grid["rows"]
        x0 = Fraction(grid["x0"])
        y0 = Fraction(grid["y0"])
        size = Fraction(grid["cell_size"])
        z_min = Fraction(plan["z_min"])
        z_max = Fraction(plan["z_max"])
        forbidden = set()
        for rect in plan["forbidden_rectangles"]:
            forbidden |= rect_cells(rect)
        passage = set()
        for rect in plan["pass_rectangles"]:
            passage |= rect_cells(rect)
        passage -= forbidden
        cell_sequences = {}
        for entry in view["points"]:
            x, y, z = (Fraction(cell) for cell in entry["xyz"])
            if not z_min <= z < z_max:
                continue
            column = (x - x0) // size
            row = (y - y0) // size
            if 0 <= column < columns and 0 <= row < rows:
                cell_sequences.setdefault((column, row), set()).add(entry["sequence"])
        accepted = {
            cell
            for cell, sequences in cell_sequences.items()
            if len(sequences) >= plan["required_sequences"]
            and cell not in forbidden
            and cell not in passage
        }
        every = {(c, r) for c in range(columns) for r in range(rows)}
        pending = every - forbidden - passage - accepted
        connectable = pending | passage
        orders = []
        remaining = set(connectable)
        while remaining:
            seed = remaining.pop()
            component = {seed}
            queue = [seed]
            while queue:
                c, r = queue.pop()
                for neighbour in ((c - 1, r), (c + 1, r), (c, r - 1), (c, r + 1)):
                    if neighbour in remaining:
                        remaining.discard(neighbour)
                        component.add(neighbour)
                        queue.append(neighbour)
            cells = component & pending
            if cells:
                orders.append(cells)
        orders.sort(key=lambda cells: min((r, c) for c, r in cells))
        return {
            "forbidden": forbidden,
            "passage": passage,
            "accepted": accepted,
            "pending": pending,
            "orders": orders,
        }

    def test_random_grids_match_reference(self):
        rng = random.Random(196)
        for trial in range(60):
            columns = rng.randint(1, 8)
            rows = rng.randint(1, 8)
            required = rng.randint(1, 3)

            def random_rect():
                left = rng.randint(0, columns - 1)
                bottom = rng.randint(0, rows - 1)
                return [
                    left,
                    bottom,
                    rng.randint(left + 1, columns),
                    rng.randint(bottom + 1, rows),
                ]

            forbidden = [random_rect() for _ in range(rng.randint(0, 4))]
            passage = [random_rect() for _ in range(rng.randint(0, 4))]
            points = {}
            for index in range(rng.randint(0, 12)):
                sequence = rng.randint(0, 3)
                # Some points land off-grid or outside the elevation band.
                x = Fraction(rng.randint(-2, 2 * columns + 1), 2)
                y = Fraction(rng.randint(-2, 2 * rows + 1), 2)
                z = Fraction(rng.randint(-1, 4), 2)
                points[(sequence, f"t{index}")] = [
                    str(x) if x.denominator == 1 else f"{x.numerator}/{x.denominator}",
                    str(y) if y.denominator == 1 else f"{y.numerator}/{y.denominator}",
                    str(z) if z.denominator == 1 else f"{z.numerator}/{z.denominator}",
                ]
            view = view_document(points)
            plan = plan_document(
                view, columns, rows, required=required,
                pass_rectangles=passage, forbidden_rectangles=forbidden,
                z_min="0", z_max="1.5",
            )
            result, report, _ = self.resurvey(plan, view, name=f"rand{trial}")
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = self.reference(plan, view)
            self.assertEqual(
                report["cells"],
                {
                    "accepted": str(len(expected["accepted"])),
                    "pending": str(len(expected["pending"])),
                    "passage": str(len(expected["passage"])),
                    "forbidden": str(len(expected["forbidden"])),
                    "total": str(columns * rows),
                },
                f"trial {trial}",
            )
            self.assertEqual(len(report["orders"]), len(expected["orders"]))
            for order, cells in zip(report["orders"], expected["orders"]):
                ident_row, ident_col = min((r, c) for c, r in cells)
                self.assertEqual(order["identity"], [ident_col, ident_row])
                self.assertEqual(order["pending_cells"], str(len(cells)))
                self.assertEqual(
                    order["bounding_box"],
                    [
                        min(c for c, _ in cells),
                        min(r for _, r in cells),
                        max(c for c, _ in cells) + 1,
                        max(r for _, r in cells) + 1,
                    ],
                )
                self.assertEqual(order_cells(order), cells, f"trial {trial}")


class CapacityTests(ResurveyCase):
    """10^9 x 10^9 grid with 20,002+1 obstacle rectangles and an empty view."""

    K = 500_000_000
    N = 10**9

    def capacity_inputs(self, gap_forbidden):
        view = view_document({}, dataset_id="survey-cap")
        forbidden = [
            [self.K, 0, self.K + 1, self.K],
            [self.K, self.K + 1, self.K + 1, self.N],
        ]
        forbidden += [
            [2 * i + 2, 2 * i + 2, 2 * i + 3, 2 * i + 3] for i in range(20_000)
        ]
        if gap_forbidden:
            forbidden.append([self.K, self.K, self.K + 1, self.K + 1])
        plan = plan_document(
            view, self.N, self.N, required=1,
            pass_rectangles=[[self.K, self.K, self.K + 1, self.K + 1]],
            forbidden_rectangles=forbidden,
            zone_id="zone-cap",
        )
        return view, plan

    def check_order_rectangles(self, report, obstacles):
        """Rectangles stay in-grid, never overlap, never touch obstacles."""
        n = self.N
        for order in report["orders"]:
            rects = order["rectangles"]
            for left, bottom, right, top in rects:
                self.assertTrue(0 <= left < right <= n)
                self.assertTrue(0 <= bottom < top <= n)
            plain = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
            self.assertEqual(plain, int(order["pending_cells"]))
            # Pairwise disjoint: union area equals the plain sum.
            self.assertEqual(union_area(rects), plain)
            # No rectangle covers any forbidden or passage cell.
            both = rects + obstacles
            self.assertEqual(
                union_area(both), union_area(rects) + union_area(obstacles)
            )

    def run_capacity(self, plan, view, name):
        started_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.perf_counter()
        result, report, _ = self.resurvey(plan, view, name=name)
        elapsed = time.perf_counter() - started
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (usage.ru_utime - started_cpu.ru_utime) + (
            usage.ru_stime - started_cpu.ru_stime
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(cpu, 20, f"resurvey took {cpu:.1f}s CPU")
        self.assertLess(elapsed, 60, f"resurvey took {elapsed:.1f}s wall")
        return report

    def test_gap_passage_keeps_one_order(self):
        view, plan = self.capacity_inputs(gap_forbidden=False)
        report = self.run_capacity(plan, view, "cap-open")
        self.assertEqual(
            report["cells"],
            {"accepted": "0", "pending": "999999998999980000",
             "passage": "1", "forbidden": "1000019999",
             "total": "1000000000000000000"},
        )
        self.assertEqual(len(report["orders"]), 1)
        order = report["orders"][0]
        self.assertEqual(order["identity"], [0, 0])
        self.assertEqual(order["pending_cells"], "999999998999980000")
        self.assertEqual(order["bounding_box"], [0, 0, self.N, self.N])
        obstacles = plan["forbidden_rectangles"] + plan["pass_rectangles"]
        self.check_order_rectangles(report, obstacles)

    def test_gap_forbidden_splits_two_orders(self):
        view, plan = self.capacity_inputs(gap_forbidden=True)
        report = self.run_capacity(plan, view, "cap-closed")
        self.assertEqual(
            report["cells"],
            {"accepted": "0", "pending": "999999998999980000",
             "passage": "0", "forbidden": "1000020000",
             "total": "1000000000000000000"},
        )
        self.assertEqual(len(report["orders"]), 2)
        left, right = report["orders"]
        self.assertEqual(left["identity"], [0, 0])
        self.assertEqual(left["pending_cells"], "499999999999980000")
        self.assertEqual(left["bounding_box"], [0, 0, self.K, self.N])
        self.assertEqual(right["identity"], [self.K + 1, 0])
        self.assertEqual(right["pending_cells"], "499999999000000000")
        self.assertEqual(right["bounding_box"], [self.K + 1, 0, self.N, self.N])
        obstacles = plan["forbidden_rectangles"] + plan["pass_rectangles"]
        self.check_order_rectangles(report, obstacles)


if __name__ == "__main__":
    unittest.main()
