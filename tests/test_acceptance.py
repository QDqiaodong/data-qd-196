"""Acceptance (复测位移验收) subcommand tests."""

import itertools
import json
from pathlib import Path
import random
import sys
import tempfile
import time
import unittest

from helpers import SRC, build_document, change_document, read_json, replacement, run_cli, write_json

sys.path.insert(0, str(SRC))
from camera_tracks.matching import (  # noqa: E402
    allowed_partners,
    hopcroft_karp,
    min_cost_matching,
)
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402


def view_document(points, dataset_id="base", version=0):
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


def ref_json(ref):
    return {"sequence": ref[0], "track_id": ref[1]}


def zone_config(baseline_view, resurvey_view, baseline_refs, resurvey_refs,
                candidates, confirmations=(), limit="2", zone_id="zone-7"):
    return {
        "schema_version": 1,
        "kind": "camera-tracks-acceptance",
        "zone_id": zone_id,
        "displacement_limit": limit,
        "baseline": endpoint(baseline_view),
        "resurvey": endpoint(resurvey_view),
        "baseline_required": [ref_json(r) for r in baseline_refs],
        "resurvey_required": [ref_json(r) for r in resurvey_refs],
        "candidates": [
            {"baseline": ref_json(a), "resurvey": ref_json(b)}
            for a, b in candidates
        ],
        "confirmations": [
            {"baseline": ref_json(a), "resurvey": ref_json(b)}
            for a, b in confirmations
        ],
    }


def axis_views(baseline_xs, resurvey_xs, **kwargs):
    baseline = view_document(
        {(i, "p"): [x, "0", "0"] for i, x in enumerate(baseline_xs)},
        dataset_id=kwargs.get("baseline_dataset", "base"),
        version=kwargs.get("baseline_version", 0),
    )
    resurvey = view_document(
        {(i, "q"): [x, "0", "0"] for i, x in enumerate(resurvey_xs)},
        dataset_id=kwargs.get("resurvey_dataset", "re"),
        version=kwargs.get("resurvey_version", 0),
    )
    n = len(baseline_xs)
    return baseline, resurvey, [(i, "p") for i in range(n)], [(i, "q") for i in range(n)]


def brute_force_min_max(baseline_refs, resurvey_refs, candidates, confirmations,
                        exceeded):
    """Enumerate every complete correspondence; None when none exists."""
    index_a = {r: i for i, r in enumerate(baseline_refs)}
    index_b = {r: i for i, r in enumerate(resurvey_refs)}
    edges = {(index_a[a], index_b[b]) for a, b in candidates}
    confirmed = {(index_a[a], index_b[b]) for a, b in confirmations}
    flags = {(index_a[a], index_b[b]): flag for (a, b), flag in exceeded.items()}
    n = len(baseline_refs)
    confirmed_a = {a for a, _ in confirmed}
    confirmed_b = {b for _, b in confirmed}
    remaining_a = [a for a in range(n) if a not in confirmed_a]
    remaining_b = [b for b in range(n) if b not in confirmed_b]
    base = sum(1 for edge in confirmed if flags[edge])
    best = worst = None
    for perm in itertools.permutations(remaining_b):
        pairs = list(zip(remaining_a, perm))
        if all(pair in edges for pair in pairs):
            score = base + sum(1 for pair in pairs if flags[pair])
            best = score if best is None else min(best, score)
            worst = score if worst is None else max(worst, score)
    return None if best is None else (best, worst)


class MatchingStressTests(unittest.TestCase):
    """Min-cost flow and allowed-edge analysis against brute force."""

    def test_random_graphs_match_brute_force(self):
        rng = random.Random(196)
        for _ in range(300):
            n = rng.randint(1, 5)
            edges = sorted(
                (a, b)
                for a in range(n)
                for b in range(n)
                if rng.random() < 0.55
            )
            adjacency = [[] for _ in range(n)]
            for a, b in edges:
                adjacency[a].append(b)
            costs = [
                [rng.randint(0, 1) for _ in row] for row in adjacency
            ]
            cost_of = {
                (a, b): costs[a][k]
                for a in range(n)
                for k, b in enumerate(adjacency[a])
            }
            match_a, match_b = hopcroft_karp(adjacency, n)
            size = sum(1 for m in match_a if m != -1)
            # Brute-force maximum matching size.
            best = 0
            for perm in itertools.permutations(range(n)):
                best = max(
                    best,
                    sum((a, b) in set(edges) for a, b in enumerate(perm)),
                )
            self.assertEqual(size, best)
            result = min_cost_matching(adjacency, costs, n)
            if size < n:
                self.assertIsNone(result)
                continue
            # Brute-force minimum cost and allowed edges over perfect ones.
            min_cost = None
            allowed = {a: set() for a in range(n)}
            for perm in itertools.permutations(range(n)):
                if all((a, b) in cost_of for a, b in enumerate(perm)):
                    total = sum(cost_of[(a, b)] for a, b in enumerate(perm))
                    min_cost = total if min_cost is None else min(min_cost, total)
                    for a, b in enumerate(perm):
                        allowed[a].add(b)
            self.assertIsNotNone(result)
            self.assertEqual(result[0], min_cost)
            self.assertEqual(sum(cost_of[(a, result[1][a])] for a in range(n)), min_cost)
            allowed_computed = allowed_partners(adjacency, match_a, match_b)
            for a in range(n):
                self.assertEqual(set(allowed_computed[a]), allowed[a])


class AcceptanceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def accept(self, config, baseline_view, resurvey_view, check=True, name="zone"):
        config_path = write_json(self.root / f"{name}.config.json", config)
        baseline_path = write_json(self.root / f"{name}.baseline.json", baseline_view)
        resurvey_path = write_json(self.root / f"{name}.resurvey.json", resurvey_view)
        output = self.root / f"{name}.report.json"
        result = run_cli(
            "accept", "--baseline", baseline_path, "--resurvey", resurvey_path,
            "--config", config_path, "--output", output, check=check,
        )
        report = read_json(output) if output.exists() else None
        return result, report, output

    def check_witness(self, witness, config, candidates, confirmations):
        """A witness must be a complete correspondence attaining its count."""
        def ref(entry):
            return (entry["sequence"], entry["track_id"])

        baseline_refs = {ref(r) for r in config["baseline_required"]}
        resurvey_refs = {ref(r) for r in config["resurvey_required"]}
        pairs = [(ref(entry["baseline"]), ref(entry["resurvey"]))
                 for entry in witness["correspondence"]]
        self.assertEqual(len(pairs), len(baseline_refs))
        self.assertEqual({a for a, _ in pairs}, baseline_refs)
        self.assertEqual({b for _, b in pairs}, resurvey_refs)
        self.assertTrue(set(pairs) <= set(candidates))
        self.assertTrue(set(confirmations) <= set(pairs))
        counted = sum(1 for entry in witness["correspondence"] if entry["exceeded"])
        self.assertEqual(counted, witness["count"])


class AxisAcceptanceTests(AcceptanceCase):
    """The two same-axis scenarios from the acceptance list."""

    def test_any_complete_correspondence_has_exactly_one_exceeded(self):
        # Baseline at x=0,1; resurvey at x=0,5; limit 2; all four candidates.
        baseline, resurvey, brefs, rrefs = axis_views(["0", "1"], ["0", "5"])
        candidates = [(a, b) for a in brefs for b in rrefs]
        config = zone_config(baseline, resurvey, brefs, rrefs, candidates)
        result, report, _ = self.accept(config, baseline, resurvey)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["conclusion"], "fail")
        self.assertTrue(report["complete"])
        # Each point alone is undecidable, but never 0..2 and never pending.
        self.assertEqual(
            [p["verdict"] for p in report["points"]], ["depends", "depends"]
        )
        for point in report["points"]:
            self.assertEqual(len(point["partners"]), 2)
        self.assertEqual(report["exceeded"]["min"]["count"], 1)
        self.assertEqual(report["exceeded"]["max"]["count"], 1)
        self.assertEqual(report["conclusion"], "fail")
        exceeded = {
            ((0, "p"), (0, "q")): False, ((0, "p"), (1, "q")): True,
            ((1, "p"), (0, "q")): False, ((1, "p"), (1, "q")): True,
        }
        self.assertEqual(brute_force_min_max(brefs, rrefs, candidates, [], exceeded), (1, 1))
        self.check_witness(report["exceeded"]["min"], config, candidates, [])
        self.check_witness(report["exceeded"]["max"], config, candidates, [])

    def test_confirmations_move_the_exceeded_range(self):
        # Baseline at x=0,4; resurvey at x=1,3; limit 2; all four candidates.
        baseline, resurvey, brefs, rrefs = axis_views(["0", "4"], ["1", "3"])
        candidates = [(a, b) for a in brefs for b in rrefs]
        exceeded = {
            ((0, "p"), (0, "q")): False, ((0, "p"), (1, "q")): True,
            ((1, "p"), (0, "q")): True, ((1, "p"), (1, "q")): False,
        }

        config = zone_config(baseline, resurvey, brefs, rrefs, candidates)
        _, report, _ = self.accept(config, baseline, resurvey, name="open")
        self.assertEqual(
            (report["exceeded"]["min"]["count"], report["exceeded"]["max"]["count"]),
            (0, 2),
        )
        self.assertEqual(report["conclusion"], "verify_labels")
        self.assertEqual(brute_force_min_max(brefs, rrefs, candidates, [], exceeded), (0, 2))
        self.check_witness(report["exceeded"]["min"], config, candidates, [])
        self.check_witness(report["exceeded"]["max"], config, candidates, [])

        # Confirming 0->1 forces the clean correspondence: signable.
        confirmations = [((0, "p"), (0, "q"))]
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates, confirmations
        )
        _, report, _ = self.accept(config, baseline, resurvey, name="confirmed-ok")
        self.assertEqual(
            (report["exceeded"]["min"]["count"], report["exceeded"]["max"]["count"]),
            (0, 0),
        )
        self.assertEqual(report["conclusion"], "sign")
        self.assertEqual(
            [p["verdict"] for p in report["points"]],
            ["never_exceeded", "never_exceeded"],
        )
        self.assertEqual(
            brute_force_min_max(brefs, rrefs, candidates, confirmations, exceeded), (0, 0)
        )
        self.check_witness(report["exceeded"]["min"], config, candidates, confirmations)
        self.check_witness(report["exceeded"]["max"], config, candidates, confirmations)

        # Confirming 0->3 forces both displacements over the limit: fail.
        confirmations = [((0, "p"), (1, "q"))]
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates, confirmations
        )
        _, report, _ = self.accept(config, baseline, resurvey, name="confirmed-bad")
        self.assertEqual(
            (report["exceeded"]["min"]["count"], report["exceeded"]["max"]["count"]),
            (2, 2),
        )
        self.assertEqual(report["conclusion"], "fail")
        self.assertEqual(
            [p["verdict"] for p in report["points"]],
            ["always_exceeded", "always_exceeded"],
        )
        self.assertEqual(
            brute_force_min_max(brefs, rrefs, candidates, confirmations, exceeded), (2, 2)
        )
        self.check_witness(report["exceeded"]["min"], config, candidates, confirmations)
        self.check_witness(report["exceeded"]["max"], config, candidates, confirmations)

    def test_candidate_edge_in_no_complete_correspondence_is_not_listed(self):
        # (b2, r0) is a candidate but b2 must take r2 in every complete
        # correspondence, so the edge must not appear among b2's partners.
        baseline, resurvey, brefs, rrefs = axis_views(["0", "1", "2"], ["0", "1", "100"])
        candidates = [
            (brefs[0], rrefs[0]), (brefs[0], rrefs[1]),
            (brefs[1], rrefs[0]), (brefs[1], rrefs[1]),
            (brefs[2], rrefs[0]), (brefs[2], rrefs[2]),
        ]
        config = zone_config(baseline, resurvey, brefs, rrefs, candidates)
        _, report, _ = self.accept(config, baseline, resurvey)
        self.assertTrue(report["complete"])
        partners = {
            (p["sequence"], p["track_id"]): [
                (q["sequence"], q["track_id"]) for q in p["partners"]
            ]
            for p in report["points"]
        }
        self.assertEqual(partners[(2, "p")], [(2, "q")])
        verdicts = {
            (p["sequence"], p["track_id"]): p["verdict"] for p in report["points"]
        }
        self.assertEqual(verdicts[(2, "p")], "always_exceeded")
        self.assertEqual(verdicts[(0, "p")], "never_exceeded")
        self.assertEqual(verdicts[(1, "p")], "never_exceeded")
        self.assertEqual(
            (report["exceeded"]["min"]["count"], report["exceeded"]["max"]["count"]),
            (1, 1),
        )
        # Only (b2, r2) exceeds the limit; (b2, r0) is exactly at it.
        exceeded = {pair: pair == (brefs[2], rrefs[2]) for pair in candidates}
        self.assertEqual(brute_force_min_max(brefs, rrefs, candidates, [], exceeded), (1, 1))

    def test_displacement_equal_to_limit_is_not_exceeded(self):
        # Exact rational coordinates: 5/3 equals the limit, 7/3 exceeds it.
        baseline, resurvey, brefs, rrefs = axis_views(["0", "10"], ["5/3", "10"])
        candidates = [(a, b) for a in brefs for b in rrefs]
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates, limit="5/3"
        )
        _, report, _ = self.accept(config, baseline, resurvey)
        flags = {
            (
                (p["sequence"], p["track_id"]),
                (q["sequence"], q["track_id"]),
            ): q["exceeded"]
            for p in report["points"]
            for q in p["partners"]
        }
        self.assertFalse(flags[((0, "p"), (0, "q"))])   # 5/3 == limit
        self.assertTrue(flags[((0, "p"), (1, "q"))])    # 10 > 5/3
        self.assertTrue(flags[((1, "p"), (0, "q"))])    # 25/3 > 5/3
        self.assertFalse(flags[((1, "p"), (1, "q"))])   # 0
        self.assertEqual(
            (report["exceeded"]["min"]["count"], report["exceeded"]["max"]["count"]),
            (0, 2),
        )

    def test_config_array_permutation_keeps_report_bytes(self):
        baseline, resurvey, brefs, rrefs = axis_views(["0", "4", "9"], ["1", "3", "9"])
        candidates = [(a, b) for a in brefs for b in rrefs]
        confirmations = [(brefs[2], rrefs[2])]
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates, confirmations, limit="2"
        )
        _, report, output = self.accept(config, baseline, resurvey, name="ordered")
        shuffled = zone_config(
            baseline, resurvey,
            list(reversed(brefs)), list(reversed(rrefs)),
            list(reversed(candidates)), list(reversed(confirmations)), limit="2",
        )
        _, shuffled_report, shuffled_output = self.accept(
            shuffled, baseline, resurvey, name="shuffled"
        )
        self.assertEqual(output.read_bytes(), shuffled_output.read_bytes())
        self.assertEqual(report, shuffled_report)


class DeficientAcceptanceTests(AcceptanceCase):
    def test_two_baseline_points_sharing_one_resurvey_point(self):
        # Three points per side; b0 and b1 can only occupy r0.
        baseline, resurvey, brefs, rrefs = axis_views(["0", "1", "2"], ["0", "1", "2"])
        candidates = [
            (brefs[0], rrefs[0]),
            (brefs[1], rrefs[0]),
            (brefs[2], rrefs[1]),
            (brefs[2], rrefs[2]),
        ]
        config = zone_config(baseline, resurvey, brefs, rrefs, candidates)
        result, report, _ = self.accept(config, baseline, resurvey)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(report["complete"])
        self.assertEqual(report["conclusion"], "incomplete_correspondence")
        # Recompute the Hall witness straight from the input: the only
        # maximum matching leaves one of {b0, b1} free, and alternating reach
        # gives S={b0,b1} with the single available neighbour r0.
        self.assertEqual(
            report["deficient"]["baseline_points"],
            [ref_json(brefs[0]), ref_json(brefs[1])],
        )
        self.assertEqual(
            report["deficient"]["available_resurvey_points"], [ref_json(rrefs[0])]
        )
        self.assertLess(
            len(report["deficient"]["available_resurvey_points"]),
            len(report["deficient"]["baseline_points"]),
        )
        # No partial displacement conclusions or exceeded counts.
        self.assertNotIn("points", report)
        self.assertNotIn("exceeded", report)

    def test_deficiency_caused_only_by_confirmation_occupancy(self):
        # Without the confirmation a complete correspondence exists; the
        # confirmed pair occupies the only resurvey point b0 can take.
        baseline, resurvey, brefs, rrefs = axis_views(["0", "1"], ["0", "1"])
        candidates = [
            (brefs[0], rrefs[0]),
            (brefs[1], rrefs[0]),
            (brefs[1], rrefs[1]),
        ]
        confirmations = [(brefs[1], rrefs[0])]
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates, confirmations
        )
        result, report, _ = self.accept(config, baseline, resurvey)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(report["complete"])
        # The deficient set holds only the unconfirmed baseline point, and
        # the confirmed resurvey point is not counted as available.
        self.assertEqual(report["deficient"]["baseline_points"], [ref_json(brefs[0])])
        self.assertEqual(report["deficient"]["available_resurvey_points"], [])
        summary = json.loads(result.stdout)
        self.assertEqual(summary["conclusion"], "incomplete_correspondence")
        self.assertEqual(summary["deficient_points"], 1)

    def test_empty_candidates_leave_every_point_unmatched(self):
        baseline, resurvey, brefs, rrefs = axis_views(["0", "1"], ["0", "1"])
        config = zone_config(baseline, resurvey, brefs, rrefs, [])
        result, report, _ = self.accept(config, baseline, resurvey)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(report["complete"])
        self.assertEqual(
            report["deficient"]["baseline_points"],
            [ref_json(brefs[0]), ref_json(brefs[1])],
        )
        self.assertEqual(report["deficient"]["available_resurvey_points"], [])

    def test_confirmations_covering_every_point(self):
        # No remaining freedom: the confirmed pairs are the only complete
        # correspondence, and the report still lists every point.
        baseline, resurvey, brefs, rrefs = axis_views(["0", "4"], ["1", "3"])
        candidates = [(a, b) for a in brefs for b in rrefs]
        confirmations = [(brefs[0], rrefs[1]), (brefs[1], rrefs[0])]
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates, confirmations, limit="2"
        )
        result, report, _ = self.accept(config, baseline, resurvey)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(report["complete"])
        self.assertEqual(
            (report["exceeded"]["min"]["count"], report["exceeded"]["max"]["count"]),
            (2, 2),
        )
        self.assertEqual(report["conclusion"], "fail")
        self.assertEqual(
            [p["verdict"] for p in report["points"]],
            ["always_exceeded", "always_exceeded"],
        )
        self.assertEqual(
            [len(p["partners"]) for p in report["points"]], [1, 1]
        )


class AcceptanceValidationTests(AcceptanceCase):
    def views(self):
        return axis_views(["0", "4"], ["1", "3"])

    def simple_config(self, **overrides):
        baseline, resurvey, brefs, rrefs = self.views()
        candidates = [(a, b) for a in brefs for b in rrefs]
        config = zone_config(baseline, resurvey, brefs, rrefs, candidates)
        config.update(overrides)
        return config, baseline, resurvey, brefs, rrefs

    def reject(self, config, baseline_view, resurvey_view, needle, name="bad"):
        result, report, output = self.accept(
            config, baseline_view, resurvey_view, check=False, name=name
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(needle, result.stderr)
        self.assertIsNone(report)
        self.assertFalse(output.exists())
        return result

    def test_wrong_version_with_same_fingerprint_is_rejected(self):
        # Two versions of one dataset share the point table and fingerprint;
        # handing in the other version's file must still fail.
        config, baseline, resurvey, _, _ = self.simple_config()
        stale = dict(baseline)
        stale["version"] = 1 - baseline["version"]
        config["baseline"] = endpoint(stale)
        self.reject(config, baseline, resurvey, "does not match the configured")

    def test_tampered_point_value_with_stale_fingerprint_is_rejected(self):
        config, baseline, resurvey, _, _ = self.simple_config()
        tampered = json.loads(json.dumps(baseline))
        tampered["points"][0]["xyz"][0] = "99"
        self.reject(config, tampered, resurvey, "does not match its point table")

    def test_configured_fingerprint_mismatch_is_rejected(self):
        config, baseline, resurvey, _, _ = self.simple_config()
        config["baseline"]["fingerprint"] = "0" * 64
        self.reject(config, baseline, resurvey, "does not match the configured")

    def test_candidate_outside_required_range_is_rejected(self):
        config, baseline, resurvey, brefs, rrefs = self.simple_config()
        config["candidates"].append(
            {"baseline": ref_json((9, "p")), "resurvey": ref_json(rrefs[0])}
        )
        self.reject(config, baseline, resurvey, "is not in the baseline required list")
        config, baseline, resurvey, brefs, rrefs = self.simple_config()
        config["candidates"].append(
            {"baseline": ref_json(brefs[0]), "resurvey": ref_json((9, "q"))}
        )
        self.reject(config, baseline, resurvey, "is not in the resurvey required list")

    def test_required_point_missing_from_view_is_rejected(self):
        config, baseline, resurvey, brefs, rrefs = self.simple_config()
        config["baseline_required"].append(ref_json((9, "p")))
        config["resurvey_required"].append(ref_json((9, "q")))
        self.reject(config, baseline, resurvey, "is not present in the baseline view")

    def test_duplicate_identities_and_pairs_are_rejected(self):
        config, baseline, resurvey, brefs, rrefs = self.simple_config()
        config["baseline_required"].append(config["baseline_required"][0])
        config["resurvey_required"].append(ref_json((9, "q")))
        self.reject(config, baseline, resurvey, "duplicate baseline_required identity")

        config, baseline, resurvey, brefs, rrefs = self.simple_config()
        config["candidates"].append(config["candidates"][0])
        self.reject(config, baseline, resurvey, "duplicate candidate")

    def test_confirmation_errors_are_rejected(self):
        baseline, resurvey, brefs, rrefs = self.views()
        candidates = [(a, b) for a in brefs for b in rrefs]
        # Confirmation that is not a candidate.
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates[:-1],
            confirmations=[candidates[-1]],
        )
        self.reject(config, baseline, resurvey, "is not among the candidates")
        # Two confirmations sharing one side's endpoint.
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates,
            confirmations=[(brefs[0], rrefs[0]), (brefs[0], rrefs[1])],
        )
        self.reject(config, baseline, resurvey, "share a baseline endpoint")
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates,
            confirmations=[(brefs[0], rrefs[0]), (brefs[1], rrefs[0])],
        )
        self.reject(config, baseline, resurvey, "share a resurvey endpoint")

    def test_unequal_required_counts_are_rejected(self):
        config, baseline, resurvey, brefs, rrefs = self.simple_config()
        config["resurvey_required"].append(ref_json((9, "q")))
        self.reject(config, baseline, resurvey, "must have the same size")

    def test_existing_report_is_never_overwritten(self):
        config, baseline, resurvey, _, _ = self.simple_config()
        output = self.root / "taken.report.json"
        output.write_text("previous report", encoding="utf-8")
        for broken in (False, True):
            candidate = json.loads(json.dumps(config)) if broken else config
            if broken:
                candidate["candidates"].append(candidate["candidates"][0])
            result = run_cli(
                "accept", "--baseline", write_json(self.root / "b.json", baseline),
                "--resurvey", write_json(self.root / "r.json", resurvey),
                "--config", write_json(self.root / "c.json", candidate),
                "--output", output, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous report")

    def test_output_must_not_point_at_an_input(self):
        config, baseline, resurvey, _, _ = self.simple_config()
        config_path = write_json(self.root / "zone.json", config)
        result = run_cli(
            "accept", "--baseline", write_json(self.root / "b.json", baseline),
            "--resurvey", write_json(self.root / "r.json", resurvey),
            "--config", config_path, "--output", config_path, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("must differ from the input", result.stderr)
        self.assertEqual(read_json(config_path), config)


class AcceptancePipelineTests(AcceptanceCase):
    """Views produced by the real delivery chain, end to end."""

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

    def test_accept_over_exported_views_and_stale_version_rejection(self):
        baseline_doc = build_document(dataset_id="site-a", sequences=(10, 20))
        resurvey_doc = build_document(dataset_id="site-b", sequences=(10, 20))
        # Resurvey points are [3,1,4] and [9,1,4] (baseline keeps [2,1,4]).
        moved = {"10": ("0.75", "0.5"), "20": ("2.25", "2")}
        for batch in resurvey_doc["batches"]:
            left_x, right_x = moved[str(batch["sequence"])]
            for observation in batch["tracks"][0]["observations"]:
                observation["xy"][0] = (
                    left_x if observation["camera_id"] == "left" else right_x
                )
        # A correction that touches no sequence leaves the view identical, so
        # version 1 of site-a has the same fingerprint as version 0.
        correction = change_document(
            "noop", 0, [replacement(40, 50, last="-1")], dataset_id="site-a"
        )
        baseline_v1 = self.build_view("base", baseline_doc, [("noop", correction)])
        baseline_v0 = self.build_view("base0", baseline_doc)
        resurvey = self.build_view("re", resurvey_doc)
        self.assertEqual(baseline_v0["fingerprint"], baseline_v1["fingerprint"])
        self.assertNotEqual(baseline_v0["version"], baseline_v1["version"])

        brefs = [(10, "marker"), (20, "marker")]
        rrefs = [(10, "marker"), (20, "marker")]
        candidates = [(a, b) for a in brefs for b in rrefs]
        config = zone_config(baseline_v1, resurvey, brefs, rrefs, candidates, limit="2")
        # The version-0 file has the right fingerprint but the wrong version.
        result, report, output = self.accept(
            config, baseline_v0, resurvey, check=False, name="stale"
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("does not match the configured", result.stderr)
        self.assertFalse(output.exists())
        # The version-1 file is accepted: baseline [2,1,4] x2, resurvey
        # [3,1,4] and [9,1,4]; every complete correspondence exceeds once.
        result, report, _ = self.accept(config, baseline_v1, resurvey, name="fresh")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(report["complete"])
        self.assertEqual(
            (report["exceeded"]["min"]["count"], report["exceeded"]["max"]["count"]),
            (1, 1),
        )
        self.assertEqual(report["conclusion"], "fail")


class BenchmarkAcceptanceTests(AcceptanceCase):
    """4096-point batch acceptance window, both field scenarios."""

    SIZE = 4096

    def benchmark(self, confirmations, name):
        n = self.SIZE
        baseline, resurvey, brefs, rrefs = axis_views(
            [str(i) for i in range(n)], [str(i) for i in range(n)],
            baseline_dataset="bench-base", resurvey_dataset="bench-re",
        )
        candidates = [(brefs[i], rrefs[i]) for i in range(n)]
        candidates += [(brefs[i], rrefs[(i + 1) % n]) for i in range(n)]
        config = zone_config(
            baseline, resurvey, brefs, rrefs, candidates, confirmations,
            limit="0", zone_id=name,
        )
        started = time.perf_counter()
        result, report, _ = self.accept(config, baseline, resurvey, name=name)
        elapsed = time.perf_counter() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 30, f"accept took {elapsed:.1f}s")
        return report, config, candidates, confirmations

    def test_open_window_every_point_has_two_partners(self):
        report, config, candidates, _ = self.benchmark((), "bench-open")
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["points"]), self.SIZE)
        for point in report["points"]:
            self.assertEqual(len(point["partners"]), 2)
            self.assertEqual(point["verdict"], "depends")
        self.assertEqual(report["exceeded"]["min"]["count"], 0)
        self.assertEqual(report["exceeded"]["max"]["count"], self.SIZE)
        self.assertEqual(report["conclusion"], "verify_labels")
        self.assertEqual(len(report["exceeded"]["min"]["correspondence"]), self.SIZE)
        self.assertEqual(len(report["exceeded"]["max"]["correspondence"]), self.SIZE)
        self.assertFalse(any(
            entry["exceeded"] for entry in report["exceeded"]["min"]["correspondence"]
        ))
        self.assertTrue(all(
            entry["exceeded"] for entry in report["exceeded"]["max"]["correspondence"]
        ))

    def test_confirmed_cross_forces_every_point_exceeded(self):
        confirmations = [((0, "p"), (1, "q"))]
        report, config, candidates, confirmations = self.benchmark(
            confirmations, "bench-fixed"
        )
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["points"]), self.SIZE)
        for point in report["points"]:
            self.assertEqual(len(point["partners"]), 1)
            self.assertEqual(point["verdict"], "always_exceeded")
        self.assertEqual(report["exceeded"]["min"]["count"], self.SIZE)
        self.assertEqual(report["exceeded"]["max"]["count"], self.SIZE)
        self.assertEqual(report["conclusion"], "fail")
        self.check_witness(report["exceeded"]["min"], config, candidates, confirmations)
        self.check_witness(report["exceeded"]["max"], config, candidates, confirmations)


if __name__ == "__main__":
    unittest.main()
