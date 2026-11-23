"""Offline origin merge (坐标归并) tests.

Covers the acceptance triangle walkthrough (including reviewed-edge locking
and a fully consistent network), the topology catalogue (parallel
contradictions, reverse-oriented ties, single bridges, two disjoint bad
cycles), endpoint and point-table binding rules, stable bytes under
reordering, publish-failure atomicity and the 60,000-fragment batch
benchmark.  Where point tables are needed they come from the real
build/export/apply/view delivery chain.
"""

import copy
import json
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import unittest

from helpers import ROOT, SRC, measure_cli, read_json, run_cli, write_json

sys.path.insert(0, str(SRC))
from camera_tracks import merge  # noqa: E402
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402

TENTATIVE = "tentative"
REVIEWED = "reviewed"
PLAN_KIND = "camera-tracks-merge-plan"
PACKAGE_KIND = "camera-tracks-merge-package"
VIEW_KIND = "camera-tracks-view"


def view_document(points, dataset_id, version=0):
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


def endpoint_of(view):
    return {
        "dataset_id": view["dataset_id"],
        "version": view["version"],
        "fingerprint": view["fingerprint"],
    }


def link(record_id, u, v, delta, status=TENTATIVE):
    return {
        "record_id": record_id,
        "from_fragment": u,
        "to_fragment": v,
        "delta": list(delta),
        "status": status,
    }


def plan_document(plan_id, baseline, fragment_ids, links, endpoint_map=None):
    endpoint_map = endpoint_map or {}
    return {
        "schema_version": 1,
        "kind": PLAN_KIND,
        "plan_id": plan_id,
        "baseline": baseline,
        "fragments": [
            {"fragment_id": fragment_id,
             "endpoint": endpoint_map.get(fragment_id)}
            for fragment_id in fragment_ids
        ],
        "links": links,
    }


class MergeHarness(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="merge-"))
        self.counter = 0

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_plan(self, plan, name="plan.json"):
        return write_json(self.root / name, plan)

    def write_view(self, fragment_id, view):
        path = self.root / f"view-{fragment_id}.json"
        return write_json(path, view), str(path)

    def run_merge(self, plan, views, output="package.json"):
        plan_path = self.write_plan(plan)
        input_map = {}
        for fragment_id, view in views.items():
            _doc, path = self.write_view(fragment_id, view)
            input_map[fragment_id] = path
        out = self.root / output
        result = run_cli(
            "merge", "--plan", str(plan_path), "--output", str(out),
            *[arg for fragment_id, path in sorted(input_map.items())
              for arg in ("--view", f"{fragment_id}={path}")],
        )
        return read_json(out), json.loads(result.stdout), out, input_map

    def assert_rejected(self, plan, views, needle):
        token = self.counter
        self.counter += 1
        plan_path = self.write_plan(plan, name=f"bad-{token}.json")
        input_map = {}
        for fragment_id, view in views.items():
            _doc, path = self.write_view(fragment_id, view)
            input_map[fragment_id] = path
        out = self.root / f"rejected-{token}.json"
        result = run_cli(
            "merge", "--plan", str(plan_path), "--output", str(out),
            *[arg for fragment_id, path in sorted(input_map.items())
              for arg in ("--view", f"{fragment_id}={path}")],
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(needle, result.stderr)
        self.assertFalse(out.exists())
        return result


def decision_documents(plan, analysis, result):
    """Compact typed-array decisions as fragment-id keyed documents."""
    documents = {}
    for node, fragment in enumerate(plan["fragments"]):
        code = result["decision_codes"][node]
        if code == 1:
            decision = merge.RELEASED
        elif code == 2:
            decision = merge.WITHHELD
        else:
            decision = merge.NOT_RELEASED
        entry = {"decision": decision}
        if code == 1:
            entry["origin_um"] = [
                result["origin_x"][node],
                result["origin_y"][node],
                result["origin_z"][node],
            ]
            entry["evidence"] = None
        elif code == 2:
            entry["origin_um"] = None

            def record_of(rank_array):
                rank = rank_array[node]
                record_id = plan["edge_record"][analysis["cand_edge"][rank]]
                return record_id

            entry["evidence"] = [
                {
                    "record_id": record_of(result["witness_rank_a"]),
                    "origin_um": [
                        result["witness_ax"][node],
                        result["witness_ay"][node],
                        result["witness_az"][node],
                    ],
                },
                {
                    "record_id": record_of(result["witness_rank_b"]),
                    "origin_um": [
                        result["witness_bx"][node],
                        result["witness_by"][node],
                        result["witness_bz"][node],
                    ],
                },
            ]
        else:
            entry["origin_um"] = None
            entry["evidence"] = None
        documents[fragment["fragment_id"]] = entry
    return documents


def triangle_links(ra_status=TENTATIVE, rb_delta=(3, 0, 0)):
    return [
        link("lRA", "R", "A", (1, 0, 0), ra_status),
        link("lAB", "A", "B", (1, 0, 0), TENTATIVE),
        link("lRB", "R", "B", rb_delta, TENTATIVE),
        link("lRC", "R", "C", (10, 0, 0), REVIEWED),
    ]


class TriangleAcceptanceTest(MergeHarness):
    """The acceptance scenario, with per-record recomputed evidence."""

    def setUp(self):
        super().setUp()
        # Two distinct fragments carry the same (sequence, track_id) and one
        # even has identical local coordinates: identities stay independent.
        self.views = {
            "R": view_document({(1, "p"): ["0", "0", "0"],
                                (2, "p"): ["1/2", "3", "0"]}, "dsR"),
            "C": view_document({(1, "p"): ["7", "0", "0"]}, "dsC"),
        }
        self.endpoint_map = {f: endpoint_of(v) for f, v in self.views.items()}
        self.fragments = ["R", "A", "B", "C"]

    def decisions_by_id(self, package):
        return {entry["fragment_id"]: entry for entry in package["fragments"]}

    def verify_evidence_recomputes(self, plan, package):
        """Every evidence position must match an independent plan re-solve."""
        loaded = merge.load_plan(str(self.root / "plan.json"))
        index = loaded["index_by_id"]
        for entry in package["fragments"]:
            if entry["evidence"] is None:
                continue
            node = index[entry["fragment_id"]]
            for evidence in entry["evidence"]:
                edge_index = loaded["edge_record"].index(evidence["record_id"])
                px, py, pz = merge.solve_without(loaded, edge_index)
                self.assertEqual(
                    tuple(evidence["origin_um"]),
                    (px[node], py[node], pz[node]),
                )

    def test_all_tentative_triangle_withholds_a_and_b(self):
        plan = plan_document(
            "plan-tri", "R", self.fragments, triangle_links(), self.endpoint_map
        )
        package, summary, _out, _inputs = self.run_merge(plan, self.views)
        self.assertEqual(summary["status"], "resolved")
        self.assertEqual(summary["feasible_discards"], ["lAB", "lRA", "lRB"])
        decisions = self.decisions_by_id(package)
        self.assertEqual(decisions["R"]["decision"], merge.RELEASED)
        self.assertEqual(decisions["R"]["origin_um"], [0, 0, 0])
        self.assertEqual(decisions["C"]["decision"], merge.RELEASED)
        self.assertEqual(decisions["C"]["origin_um"], [10, 0, 0])
        self.assertEqual(decisions["A"]["decision"], merge.WITHHELD)
        self.assertEqual(decisions["B"]["decision"], merge.WITHHELD)
        # Two distinct, recomputable positions, never a bare "conflict" label.
        for fragment_id in ("A", "B"):
            evidence = decisions[fragment_id]["evidence"]
            self.assertEqual(len(evidence), 2)
            self.assertNotEqual(
                evidence[0]["origin_um"], evidence[1]["origin_um"]
            )
            for item in evidence:
                self.assertEqual(set(item), {"record_id", "origin_um", "origin_m"})
        # The two evidence positions are fixed by the tie values, while the
        # stable witness record ids may depend on the spanning-tree choice;
        # verify_evidence_recomputes checks every id against an independent
        # solve below, here we pin the exact engineering positions.
        evidence_a = {tuple(e["origin_um"]) for e in decisions["A"]["evidence"]}
        self.assertEqual(evidence_a, {(1, 0, 0), (2, 0, 0)})
        evidence_b = {tuple(e["origin_um"]) for e in decisions["B"]["evidence"]}
        self.assertEqual(evidence_b, {(2, 0, 0), (3, 0, 0)})
        for fragment_id in ("A", "B"):
            for evidence in decisions[fragment_id]["evidence"]:
                self.assertIn(
                    evidence["record_id"], summary["feasible_discards"]
                )
        self.verify_evidence_recomputes(plan, package)
        # Only released, table-bearing fragments feed the merged set; local
        # coordinates are translated by exact rational T in metres.
        delivered = {(p["fragment_id"], p["sequence"], p["track_id"]): p["xyz"]
                     for p in package["points"]}
        self.assertEqual(
            set(delivered),
            {("R", 1, "p"), ("R", 2, "p"), ("C", 1, "p")},
        )
        self.assertEqual(delivered[("R", 1, "p")], ["0", "0", "0"])
        self.assertEqual(delivered[("R", 2, "p")], ["1/2", "3", "0"])
        # 7 m local + 10 um origin = 7 + 1/100000 m, lowest terms.
        self.assertEqual(delivered[("C", 1, "p")], ["700001/100000", "0", "0"])
        self.assertEqual(package["point_count"], 3)
        self.assertEqual(package["released_count"], 2)
        self.assertEqual(package["withheld_count"], 2)
        self.assertEqual(package["not_released_count"], 0)
        self.assertEqual(package["baseline"], "R")
        self.assertEqual(package["plan_id"], "plan-tri")

    def test_reviewed_ra_locks_a_but_b_stays_withheld(self):
        plan = plan_document(
            "plan-tri", "R", self.fragments,
            triangle_links(ra_status=REVIEWED), self.endpoint_map,
        )
        package, summary, _out, _inputs = self.run_merge(plan, self.views)
        self.assertEqual(summary["feasible_discards"], ["lAB", "lRB"])
        decisions = self.decisions_by_id(package)
        self.assertEqual(decisions["A"]["decision"], merge.RELEASED)
        self.assertEqual(decisions["A"]["origin_um"], [1, 0, 0])
        self.assertEqual(decisions["B"]["decision"], merge.WITHHELD)
        self.assertEqual(decisions["R"]["decision"], merge.RELEASED)
        self.assertEqual(decisions["C"]["decision"], merge.RELEASED)
        # A now joins the delivered set with T(A) = 1 um = 1/100000 m.
        delivered = {p["fragment_id"] for p in package["points"]}
        self.assertEqual(delivered, {"R", "C"})  # A has no bound table
        self.verify_evidence_recomputes(plan, package)
        for evidence in decisions["B"]["evidence"]:
            self.assertIn(evidence["record_id"], ("lAB", "lRB"))

    def test_consistent_network_releases_everything_without_candidates(self):
        plan = plan_document(
            "plan-tri", "R", self.fragments,
            triangle_links(ra_status=REVIEWED, rb_delta=(2, 0, 0)),
            self.endpoint_map,
        )
        package, summary, _out, _inputs = self.run_merge(plan, self.views)
        self.assertEqual(summary["status"], "consistent")
        self.assertEqual(summary["feasible_discards"], [])
        for entry in package["fragments"]:
            self.assertEqual(entry["decision"], merge.RELEASED)
            self.assertIsNone(entry["evidence"])
        origins = self.decisions_by_id(package)
        self.assertEqual(origins["A"]["origin_um"], [1, 0, 0])
        self.assertEqual(origins["B"]["origin_um"], [2, 0, 0])
        self.assertEqual(origins["C"]["origin_um"], [10, 0, 0])


class TopologyTest(MergeHarness):
    def analyze(self, fragment_ids, links, baseline="R"):
        plan_path = write_json(
            self.root / f"plan-{id(links)}.json",
            plan_document("p", baseline, fragment_ids, links),
        )
        loaded = merge.load_plan(str(plan_path))
        return merge.analyze(loaded), loaded

    def candidate_ids(self, analysis, loaded):
        return sorted(
            loaded["edge_record"][analysis["cand_edge"][rank]]
            for rank in range(len(analysis["cand_edge"]))
        )

    def decision_map(self, loaded, analysis):
        return decision_documents(
            loaded, analysis, merge.decide_fragments(loaded, analysis)
        )

    def test_parallel_contradictory_ties(self):
        analysis, loaded = self.analyze(
            ["R", "A"],
            [link("e1", "R", "A", (1, 0, 0)),
             link("e2", "R", "A", (2, 0, 0))],
        )
        self.assertEqual(analysis["status"], merge.RESOLVED)
        self.assertEqual(self.candidate_ids(analysis, loaded), ["e1", "e2"])
        by_id = self.decision_map(loaded, analysis)
        self.assertEqual(by_id["R"]["decision"], merge.RELEASED)
        self.assertEqual(by_id["A"]["decision"], merge.WITHHELD)
        evidence = {e["record_id"]: e["origin_um"]
                    for e in by_id["A"]["evidence"]}
        # Discarding e1 leaves e2 (T=2); discarding e2 leaves e1 (T=1).
        self.assertEqual(evidence, {"e1": [2, 0, 0], "e2": [1, 0, 0]})

    def test_reverse_oriented_tie_negates_components(self):
        analysis, loaded = self.analyze(
            ["R", "A"],
            [link("e1", "A", "R", (-1, 0, 0)),
             link("e2", "R", "A", (2, 0, 0))],
        )
        self.assertEqual(self.candidate_ids(analysis, loaded), ["e1", "e2"])
        # A consistent reverse-oriented tie introduces no contradiction.
        ok, loaded_ok = self.analyze(
            ["R", "A"],
            [link("e1", "A", "R", (-1, 0, 0)),
             link("e2", "R", "A", (1, 0, 0))],
        )
        self.assertEqual(ok["status"], merge.CONSISTENT)
        px, py, pz = ok["positions"]
        self.assertEqual((px[1], py[1], pz[1]), (1, 0, 0))

    def test_reviewed_tie_is_never_a_candidate(self):
        analysis, loaded = self.analyze(
            ["R", "A"],
            [link("e1", "R", "A", (1, 0, 0), REVIEWED),
             link("e2", "R", "A", (2, 0, 0))],
        )
        self.assertEqual(self.candidate_ids(analysis, loaded), ["e2"])
        both_reviewed, loaded_r = self.analyze(
            ["R", "A"],
            [link("e1", "R", "A", (1, 0, 0), REVIEWED),
             link("e2", "R", "A", (2, 0, 0), REVIEWED)],
        )
        self.assertEqual(both_reviewed["status"], merge.UNEXPLAINED)
        self.assertEqual(self.candidate_ids(both_reviewed, loaded_r), [])

    def test_single_bridge_cannot_be_discarded(self):
        # A hangs from R through one tentative bridge; an unrelated bad
        # triangle must never make that bridge a feasible discard.
        analysis, loaded = self.analyze(
            ["R", "A", "B", "C"],
            [
                link("bridge", "R", "A", (4, 0, 0)),
                link("rb", "R", "B", (1, 0, 0)),
                link("bc", "B", "C", (1, 0, 0)),
                link("rc", "R", "C", (9, 0, 0)),
            ],
        )
        candidates = self.candidate_ids(analysis, loaded)
        self.assertNotIn("bridge", candidates)
        self.assertEqual(set(candidates), {"rb", "bc", "rc"})
        # A keeps its bridge position under every feasible discard.
        by_id = self.decision_map(loaded, analysis)
        self.assertEqual(by_id["A"]["decision"], merge.RELEASED)
        self.assertEqual(by_id["A"]["origin_um"], [4, 0, 0])

    def test_two_disjoint_bad_cycles_have_no_single_repair(self):
        analysis, loaded = self.analyze(
            ["R", "A", "B", "C", "D"],
            [
                link("ra", "R", "A", (1, 0, 0)),
                link("ab", "A", "B", (1, 0, 0)),
                link("rb", "R", "B", (3, 0, 0)),
                link("rc", "R", "C", (1, 0, 0)),
                link("cd", "C", "D", (1, 0, 0)),
                link("rd", "R", "D", (3, 0, 0)),
            ],
        )
        self.assertEqual(analysis["status"], merge.UNEXPLAINED)
        self.assertEqual(self.candidate_ids(analysis, loaded), [])
        by_id = self.decision_map(loaded, analysis)
        self.assertTrue(
            all(d["decision"] == merge.NOT_RELEASED for d in by_id.values())
        )
        self.assertTrue(all(d["origin_um"] is None for d in by_id.values()))
        self.assertTrue(all(d["evidence"] is None for d in by_id.values()))

    def test_unexplained_package_publishes_no_points_even_for_baseline(self):
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        plan = plan_document(
            "p", "R", ["R", "A", "B", "C", "D"],
            [
                link("ra", "R", "A", (1, 0, 0)),
                link("ab", "A", "B", (1, 0, 0)),
                link("rb", "R", "B", (3, 0, 0)),
                link("rc", "R", "C", (1, 0, 0)),
                link("cd", "C", "D", (1, 0, 0)),
                link("rd", "R", "D", (3, 0, 0)),
            ],
            {"R": endpoint_of(view_r)},
        )
        package, summary, _out, _inputs = self.run_merge(plan, {"R": view_r})
        self.assertEqual(summary["status"], "unexplained")
        self.assertEqual(package["points"], [])
        self.assertEqual(package["point_count"], 0)
        self.assertEqual(package["not_released_count"], 5)
        for entry in package["fragments"]:
            self.assertEqual(entry["decision"], merge.NOT_RELEASED)


class InputBindingTest(MergeHarness):
    def triangle_plan(self, endpoint_map, fragments=("R", "A", "B")):
        return plan_document(
            "p", "R", list(fragments),
            [link("ra", "R", "A", (1, 0, 0)),
             link("ab", "A", "B", (1, 0, 0)),
             link("rb", "R", "B", (2, 0, 0))],
            endpoint_map,
        )

    def test_wrong_endpoint_version_with_equal_fingerprint_rejected(self):
        view = view_document({(1, "p"): ["0", "0", "0"]}, "dsR", version=0)
        planned = endpoint_of(view)
        wrong = copy.deepcopy(planned)
        wrong["version"] = 1  # fingerprint deliberately unchanged
        plan = self.triangle_plan({"R": wrong})
        self.assert_rejected(plan, {"R": view}, "does not match")

    def test_wrong_dataset_and_fingerprint_rejected(self):
        view = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        wrong_dataset = copy.deepcopy(endpoint_of(view))
        wrong_dataset["dataset_id"] = "dsOTHER"
        self.assert_rejected(
            self.triangle_plan({"R": wrong_dataset}), {"R": view},
            "dataset_id",
        )
        wrong_fp = copy.deepcopy(endpoint_of(view))
        wrong_fp["fingerprint"] = "a" * 64
        self.assert_rejected(
            self.triangle_plan({"R": wrong_fp}), {"R": view}, "fingerprint"
        )

    def test_tampered_withheld_fragment_table_is_still_validated(self):
        # R is released but A is withheld; tampering A's table must still fail.
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        view_a = view_document({(1, "q"): ["0", "0", "0"]}, "dsA")
        endpoint_map = {"R": endpoint_of(view_r), "A": endpoint_of(view_a)}
        plan = plan_document(
            "p", "R", ["R", "A", "B"],
            [link("ra", "R", "A", (1, 0, 0)),
             link("ab", "A", "B", (1, 0, 0)),
             link("rb", "R", "B", (3, 0, 0))],
            endpoint_map,
        )
        tampered = copy.deepcopy(view_a)
        tampered["points"][0]["xyz"] = ["9", "9", "9"]  # fingerprint stays stale
        self.assert_rejected(plan, {"R": view_r, "A": tampered}, "fingerprint")

    def test_missing_and_extra_view_inputs_rejected(self):
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        view_a = view_document({(1, "q"): ["0", "0", "0"]}, "dsA")
        plan = self.triangle_plan(
            {"R": endpoint_of(view_r), "A": endpoint_of(view_a)}
        )
        # Missing A.
        self.assert_rejected(plan, {"R": view_r}, "missing view input")
        # Extra, unbound fragment id.
        plan_path = self.write_plan(plan, name="extra-plan.json")
        _doc, r_path = self.write_view("R", view_r)
        _doc, a_path = self.write_view("A", view_a)
        out = self.root / "extra-out.json"
        result = run_cli(
            "merge", "--plan", str(plan_path), "--output", str(out),
            "--view", f"R={r_path}", "--view", f"A={a_path}",
            "--view", f"GHOST={a_path}", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("without a planned endpoint", result.stderr)
        self.assertFalse(out.exists())

    def test_disconnected_plan_is_invalid_not_a_tie_failure(self):
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        plan = plan_document(
            "p", "R", ["R", "A", "B"],
            [link("ra", "R", "A", (1, 0, 0)),
             link("bb", "B", "B", (0, 0, 0))],
            {"R": endpoint_of(view_r)},
        )
        # The self-link is itself illegal; build a truly disconnected one too.
        self.assert_rejected(plan, {"R": view_r}, "different fragments")
        plan_ok = plan_document(
            "p", "R", ["R", "A", "B"],
            [link("ra", "R", "A", (1, 0, 0))],
            {"R": endpoint_of(view_r)},
        )
        self.assert_rejected(plan_ok, {"R": view_r}, "do not connect")

    def test_duplicate_and_malformed_structure_rejected(self):
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        base = self.triangle_plan({"R": endpoint_of(view_r)})
        # Duplicate fragment id.
        bad = copy.deepcopy(base)
        bad["fragments"].append(copy.deepcopy(bad["fragments"][0]))
        self.assert_rejected(bad, {"R": view_r}, "duplicate fragment id")
        # Duplicate record id.
        bad = copy.deepcopy(base)
        bad["links"].append(copy.deepcopy(bad["links"][0]))
        self.assert_rejected(bad, {"R": view_r}, "duplicate link record id")
        # Out-of-range micron component.
        bad = copy.deepcopy(base)
        bad["links"][0]["delta"][0] = 10**12 + 1
        self.assert_rejected(bad, {"R": view_r}, "integer")
        # Unknown fragment reference.
        bad = copy.deepcopy(base)
        bad["links"][0]["to_fragment"] = "ZZ"
        self.assert_rejected(bad, {"R": view_r}, "unknown fragment")

    def test_identical_point_identities_never_merge(self):
        # Same (sequence, track_id) and identical local XYZ in two fragments.
        points = {(7, "mark"): ["1", "2", "3"]}
        view_r = view_document(points, "dsR")
        view_a = view_document(copy.deepcopy(points), "dsA")
        plan = plan_document(
            "p", "R", ["R", "A"],
            [link("ra", "R", "A", (1_000_000, 2_000_000, 0), REVIEWED)],
            {"R": endpoint_of(view_r), "A": endpoint_of(view_a)},
        )
        package, _summary, _out, _inputs = self.run_merge(
            plan, {"R": view_r, "A": view_a}
        )
        self.assertEqual(package["point_count"], 2)
        identities = [(p["fragment_id"], p["sequence"], p["track_id"])
                      for p in package["points"]]
        self.assertEqual(identities, [("A", 7, "mark"), ("R", 7, "mark")])
        by_fragment = {p["fragment_id"]: p["xyz"] for p in package["points"]}
        self.assertEqual(by_fragment["R"], ["1", "2", "3"])
        self.assertEqual(by_fragment["A"], ["2", "4", "3"])  # + (1m, 2m, 0)

    def test_single_fragment_plan_without_links_is_consistent(self):
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        plan = plan_document(
            "p", "R", ["R"], [], {"R": endpoint_of(view_r)}
        )
        package, summary, _out, _inputs = self.run_merge(plan, {"R": view_r})
        self.assertEqual(summary["status"], "consistent")
        self.assertEqual(package["fragments"][0]["origin_um"], [0, 0, 0])
        self.assertEqual(package["point_count"], 1)

    def test_coordinate_only_node_needs_no_view_and_still_decides(self):
        # B has no endpoint/view; it still participates as a transfer node.
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        plan = plan_document(
            "p", "R", ["R", "A", "B"],
            [link("ra", "R", "A", (1_000_000, 0, 0), REVIEWED),
             link("ab", "A", "B", (2_000_000, 0, 0), REVIEWED)],
            {"R": endpoint_of(view_r)},
        )
        package, _summary, _out, _inputs = self.run_merge(plan, {"R": view_r})
        by_id = {f["fragment_id"]: f for f in package["fragments"]}
        self.assertIsNone(by_id["B"]["endpoint"])
        self.assertFalse(by_id["B"]["has_table"])
        self.assertEqual(by_id["B"]["origin_um"], [3_000_000, 0, 0])
        self.assertEqual(package["endpoints"], [
            {
                "fragment_id": "R",
                "dataset_id": view_r["dataset_id"],
                "version": view_r["version"],
                "fingerprint": view_r["fingerprint"],
            }
        ])

    def test_malformed_cli_binding_rejected(self):
        view_r = view_document({(1, "p"): ["0", "0", "0"]}, "dsR")
        plan = self.triangle_plan({"R": endpoint_of(view_r)})
        plan_path = self.write_plan(plan, name="binding-plan.json")
        out = self.root / "binding-out.json"
        result = run_cli(
            "merge", "--plan", str(plan_path), "--output", str(out),
            "--view", "no-equals-sign", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("FRAGMENT_ID=PATH", result.stderr)
        self.assertFalse(out.exists())


class StabilityAndPublishTest(MergeHarness):
    def build_inputs(self):
        view_r = view_document({(1, "p"): ["0", "0", "0"],
                                (5, "p"): ["1/3", "0", "0"]}, "dsR")
        view_c = view_document({(1, "p"): ["7", "0", "0"]}, "dsC")
        endpoint_map = {"R": endpoint_of(view_r), "C": endpoint_of(view_c)}
        plan = plan_document(
            "plan-s", "R", ["R", "A", "B", "C"], triangle_links(), endpoint_map
        )
        return plan, {"R": view_r, "C": view_c}

    def reverse_keys(self, value):
        if isinstance(value, dict):
            return {k: self.reverse_keys(value[k]) for k in reversed(list(value))}
        if isinstance(value, list):
            return [self.reverse_keys(item) for item in value]
        return value

    def test_reordering_declarations_arrays_and_view_rows_keeps_bytes(self):
        plan, views = self.build_inputs()
        package_a, _s, out_a, _i = self.run_merge(
            copy.deepcopy(plan), copy.deepcopy(views), output="a.json"
        )
        rng = random.Random(196)
        shuffled_plan = copy.deepcopy(plan)
        rng.shuffle(shuffled_plan["fragments"])
        rng.shuffle(shuffled_plan["links"])
        shuffled_plan = self.reverse_keys(shuffled_plan)
        shuffled_views = {}
        for fragment_id, view in views.items():
            view = copy.deepcopy(view)
            rng.shuffle(view["points"])
            shuffled_views[fragment_id] = self.reverse_keys(view)
        _package_b, _s, out_b, _i = self.run_merge(
            shuffled_plan, shuffled_views, output="b.json"
        )
        self.assertEqual(
            out_a.read_bytes(), out_b.read_bytes(),
            "package bytes must not depend on declaration or row order",
        )

    def test_package_digest_recomputes_from_canonical_stream(self):
        import hashlib

        plan, views = self.build_inputs()
        package, _s, _out, _inputs = self.run_merge(
            plan, views, output="digest.json"
        )

        def canonical(value):
            return json.dumps(
                value, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False, sort_keys=True,
            ).encode("utf-8")

        digest = hashlib.sha256()
        digest.update(merge.PACKAGE_FORMAT.encode("ascii"))
        digest.update(canonical({
            "plan_id": package["plan_id"],
            "baseline": package["baseline"],
            "status": package["status"],
            "feasible_discards": package["feasible_discards"],
            "endpoints": package["endpoints"],
        }))
        for fragment in package["fragments"]:
            digest.update(canonical(fragment))
        for point in package["points"]:
            digest.update(canonical(point))
        self.assertEqual(
            package["package_digest"],
            merge.PACKAGE_FORMAT + ":" + digest.hexdigest(),
        )

    def test_existing_output_and_input_target_refused(self):
        plan, views = self.build_inputs()
        plan_path = self.write_plan(plan)
        input_map = {}
        for fragment_id, view in views.items():
            _doc, path = self.write_view(fragment_id, view)
            input_map[fragment_id] = path
        occupied = self.root / "occupied.json"
        occupied.write_text("{}", encoding="utf-8")
        result = run_cli(
            "merge", "--plan", str(plan_path), "--output", str(occupied),
            *[arg for fragment_id, path in sorted(input_map.items())
              for arg in ("--view", f"{fragment_id}={path}")],
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        # Output pointing at an input is refused and the input is untouched.
        result = run_cli(
            "merge", "--plan", str(plan_path),
            "--output", str(plan_path),
            *[arg for fragment_id, path in sorted(input_map.items())
              for arg in ("--view", f"{fragment_id}={path}")],
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must differ", result.stderr)

    def test_fault_before_write_leaves_no_target(self):
        plan, views = self.build_inputs()
        plan_path = self.write_plan(plan)
        input_map = {}
        for fragment_id, view in views.items():
            _doc, path = self.write_view(fragment_id, view)
            input_map[fragment_id] = path
        out = self.root / "fault.json"
        result = run_cli(
            "merge", "--plan", str(plan_path), "--output", str(out),
            *[arg for fragment_id, path in sorted(input_map.items())
              for arg in ("--view", f"{fragment_id}={path}")],
            fault="merge-before-write", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("injected interruption", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual(
            list(self.root.glob(".publish-*")), [],
            "no temporary package may remain at the final location",
        )

    def test_inputs_are_not_modified(self):
        plan, views = self.build_inputs()
        plan_path = self.write_plan(plan)
        before = {str(plan_path): plan_path.read_bytes()}
        input_map = {}
        for fragment_id, view in views.items():
            _doc, path = self.write_view(fragment_id, view)
            before[str(path)] = Path(path).read_bytes()
            input_map[fragment_id] = str(path)
        out = self.root / "ro.json"
        run_cli(
            "merge", "--plan", str(plan_path), "--output", str(out),
            *[arg for fragment_id, path in sorted(input_map.items())
              for arg in ("--view", f"{fragment_id}={path}")],
        )
        for path, raw in before.items():
            self.assertEqual(Path(path).read_bytes(), raw)


class DeliveryChainIntegrationTest(MergeHarness):
    """Real build/export/apply/view exports feed the merge command."""

    def build_view(self, name, build_input):
        build_path = write_json(self.root / f"{name}-build.json", build_input)
        upstream = self.root / f"{name}-up.json"
        run_cli("build", "--input", str(build_path), "--store", str(upstream))
        full = self.root / f"{name}-full.json"
        run_cli("export", "--store", str(upstream), "--full", "--output", str(full))
        downstream = self.root / f"{name}-down.json"
        run_cli("apply", "--store", str(downstream), "--package", str(full))
        view_path = self.root / f"{name}-view.json"
        run_cli("view", "--store", str(downstream), "--output", str(view_path))
        return read_json(view_path), str(view_path)

    @staticmethod
    def build_input(dataset_id, sequence):
        # Two cameras intersecting at XYZ = [2,1,4] with zero residual.
        return {
            "dataset_id": dataset_id,
            "max_reprojection_error": "0",
            "cameras": [
                {"id": "left",
                 "projection": [["1", "0", "0", "0"], ["0", "1", "0", "0"],
                                ["0", "0", "1", "0"]]},
                {"id": "right",
                 "projection": [["1", "0", "0", "-1"], ["0", "1", "0", "0"],
                                ["0", "0", "1", "0"]]},
            ],
            "batches": [
                {"sequence": sequence,
                 "tracks": [
                     {"id": "marker",
                      "observations": [
                          {"camera_id": "left", "xy": ["0.5", "0.25"],
                           "weight": "1"},
                          {"camera_id": "right", "xy": ["0.25", "0.25"],
                           "weight": "1"},
                      ]}]}
            ],
        }

    def test_real_delivery_views_merge_with_exact_translation(self):
        view_r, r_path = self.build_view(
            "r", self.build_input("dsR", 10),
        )
        view_a, a_path = self.build_view(
            "a", self.build_input("dsA", 10),
        )
        # Both fragments reconstruct [2,1,4] locally; A's origin is 1 metre X.
        plan = plan_document(
            "plan-chain", "R", ["R", "A"],
            [link("ra", "R", "A", (1_000_000, 0, 0), REVIEWED)],
            {"R": endpoint_of(view_r), "A": endpoint_of(view_a)},
        )
        plan_path = self.write_plan(plan)
        out = self.root / "merged.json"
        run_cli(
            "merge", "--plan", str(plan_path), "--output", str(out),
            "--view", f"R={r_path}", "--view", f"A={a_path}",
        )
        package = read_json(out)
        self.assertEqual(package["kind"], PACKAGE_KIND)
        self.assertEqual(package["point_count"], 2)
        by_fragment = {p["fragment_id"]: p["xyz"] for p in package["points"]}
        self.assertEqual(by_fragment["R"], ["2", "1", "4"])
        self.assertEqual(by_fragment["A"], ["3", "1", "4"])
        # The native views remain untouched and keep their own fingerprints.
        self.assertEqual(view_r["kind"], VIEW_KIND)
        self.assertEqual(
            read_json(r_path)["fingerprint"], view_r["fingerprint"]
        )


class BatchBenchmarkTest(MergeHarness):
    """The 60,000-fragment acceptance batch, built and checked in full."""

    def make_batch(self):
        total = 60_000

        def fid(i):
            return f"V{i:05d}"

        links = []
        for i in range(1, total):
            links.append(link(f"e{i}", fid(i - 1), fid(i), (1_000_000, 0, 0)))
        links.append(link("f", fid(0), fid(total - 1),
                          (60_000_000_000, 0, 0)))
        links.append(link("g1", fid(0), fid(20_000),
                          (20_000_000_000, 0, 0), REVIEWED))
        links.append(link("g2", fid(40_000), fid(total - 1),
                          (19_999_000_000, 0, 0), REVIEWED))
        view_20k = view_document({(1, "p"): ["0", "0", "0"]}, "dsV20000")
        view_20k1 = view_document({(1, "p"): ["0", "0", "0"]}, "dsV20001")
        endpoint_map = {
            fid(20_000): endpoint_of(view_20k),
            fid(20_001): endpoint_of(view_20k1),
        }
        plan = plan_document(
            "plan-batch", fid(0), [fid(i) for i in range(total)],
            links, endpoint_map,
        )
        return plan, {fid(20_000): view_20k, fid(20_001): view_20k1}

    def test_batch_decisions_evidence_and_limits(self):
        plan, views = self.make_batch()
        package, summary, _out, _inputs = self.run_merge(
            plan, views, output="batch.json"
        )
        # Exact candidate set: f plus e_20001..e_40000 (20,000 records).
        expected = {"f"} | {f"e{i}" for i in range(20_001, 40_001)}
        self.assertEqual(set(summary["feasible_discards"]), expected)
        self.assertEqual(len(summary["feasible_discards"]), 20_001)
        self.assertEqual(summary["status"], "resolved")
        self.assertEqual(summary["released"], 20_001)   # V0..V20000
        self.assertEqual(summary["withheld"], 39_999)   # V20001..V59999
        self.assertEqual(summary["not_released"], 0)
        decisions = {entry["fragment_id"]: entry for entry in package["fragments"]}
        # Determined positions follow the chain, 1 m per step.
        for i in (0, 1, 19_999, 20_000):
            entry = decisions[f"V{i:05d}"]
            self.assertEqual(entry["decision"], merge.RELEASED)
            self.assertEqual(entry["origin_um"], [i * 1_000_000, 0, 0])
        # The two bound tables: only V20000's point is delivered.
        self.assertEqual(package["point_count"], 1)
        point = package["points"][0]
        self.assertEqual(
            (point["fragment_id"], point["sequence"], point["track_id"]),
            ("V20000", 1, "p"),
        )
        self.assertEqual(point["xyz"], ["20000", "0", "0"])
        withheld = decisions["V20001"]
        self.assertEqual(withheld["decision"], merge.WITHHELD)
        self.assertEqual(len(withheld["evidence"]), 2)
        loaded = merge.load_plan(str(self.root / "plan.json"))
        index = loaded["index_by_id"]
        total_fragments = 60_000
        edge_record = loaded["edge_record"]
        candidate_edge = {
            record_id: edge_index
            for edge_index, record_id in enumerate(edge_record)
        }

        # Independent verification written from first principles rather than
        # calling analyze(): build the BFS tree, find the three non-tree
        # records (E - (V - 1) == 3 for this graph) and their residuals.
        parent_edge, parent_node, tree_pos, _order = merge.spanning_tree(loaded)
        tin, tout = merge._subtree_intervals(parent_node)
        tree_edge_set = {e for e in parent_edge if e >= 0}
        non_tree = [
            edge_index for edge_index in range(len(edge_record))
            if edge_index not in tree_edge_set
        ]
        self.assertEqual(len(non_tree), 3)
        eu, ev = loaded["edge_u"], loaded["edge_v"]
        residual = {
            edge_index: merge._residual_at(loaded, edge_index, tree_pos)
            for edge_index in non_tree
        }
        bad_nt = [e for e, r in residual.items() if r != (0, 0, 0)]
        zero_nt = [e for e, r in residual.items() if r == (0, 0, 0)]
        self.assertEqual(len(bad_nt), 1)
        bad_edge = bad_nt[0]
        bad_residual = residual[bad_edge]
        self.assertEqual(bad_residual, (1_000_000, 0, 0))

        def inside(node, child):
            return tin[child] <= tin[node] <= tout[child]

        def crosses(edge_index, child):
            return inside(eu[edge_index], child) != inside(ev[edge_index], child)

        # Feasibility from cut arithmetic alone: a tentative tree edge works
        # iff every bad non-tree edge crosses its cut, no zero one does, and
        # all crossing bad edges demand one identical shift; a tentative
        # non-tree edge works only when it is the unique bad non-tree record.
        legal = set()
        shift_by_record = {}
        subtree_root = {}
        for edge_index, record_id in enumerate(edge_record):
            if not loaded["edge_tentative"][edge_index]:
                continue
            if edge_index == bad_edge:
                legal.add(record_id)
                shift_by_record[record_id] = (0, 0, 0)
                subtree_root[record_id] = -1
                continue
            if edge_index in zero_nt:
                continue
            child = next(
                node for node in range(total_fragments)
                if parent_edge[node] == edge_index
            )
            if any(not crosses(b, child) for b in bad_nt):
                continue
            if any(crosses(z, child) for z in zero_nt):
                continue
            required = None
            for b in bad_nt:
                shift = (
                    tuple(-c for c in residual[b])
                    if inside(ev[b], child) else residual[b]
                )
                if required is None:
                    required = shift
                elif shift != required:
                    required = None
                    break
            if required is None:
                continue
            legal.add(record_id)
            shift_by_record[record_id] = required
            subtree_root[record_id] = child
        self.assertEqual(legal, expected)

        tx, ty, tz = tree_pos

        def base_of(node):
            return (tx[node], ty[node], tz[node])

        # Exhaustive evidence check against the independently derived shift
        # rule: every withheld fragment has two distinct, recomputable
        # positions and released origins never move under any discard.
        checked = 0
        for j in range(total_fragments):
            fragment_id = f"V{j:05d}"
            entry = decisions[fragment_id]
            node = index[fragment_id]
            base = base_of(node)
            if entry["decision"] == merge.RELEASED:
                self.assertEqual(tuple(entry["origin_um"]), base)
                continue
            self.assertEqual(entry["decision"], merge.WITHHELD)
            self.assertEqual(len(entry["evidence"]), 2)
            origins = set()
            for evidence in entry["evidence"]:
                record_id = evidence["record_id"]
                self.assertIn(record_id, expected)
                shift = shift_by_record[record_id]
                root = subtree_root[record_id]
                covered = root < 0 or inside(node, root)
                want = (
                    tuple(base[a] + shift[a] for a in range(3))
                    if covered else base
                )
                self.assertEqual(tuple(evidence["origin_um"]), want)
                origins.add(want)
            self.assertEqual(len(origins), 2)
            checked += 1
        self.assertEqual(checked, 39_999)

        # Anchor the shift arithmetic with five independent full BFS solves
        # over all 60,000 rows: the non-tree candidate and tree candidates at
        # both boundaries and the middle of the discardable range.
        anchor_ids = ["f", "e20001", "e30000", "e40000"]
        if edge_record[bad_edge] not in anchor_ids:
            anchor_ids.append(edge_record[bad_edge])
        anchor_tables = {
            record_id: merge.solve_without(
                loaded, candidate_edge[record_id]
            )
            for record_id in anchor_ids
        }
        for record_id, positions in anchor_tables.items():
            shift = shift_by_record[record_id]
            root = subtree_root[record_id]
            for node in range(total_fragments):
                base = base_of(node)
                want = (
                    tuple(base[a] + shift[a] for a in range(3))
                    if root < 0 or inside(node, root) else base
                )
                got = (positions[0][node], positions[1][node], positions[2][node])
                self.assertEqual(got, want)
        # Released fragments must be invariant under every anchor discard.
        for record_id, positions in anchor_tables.items():
            for j in range(20_001):
                node = index[f"V{j:05d}"]
                self.assertEqual(
                    (positions[0][node], positions[1][node], positions[2][node]),
                    base_of(node),
                )
        # And every anchor position matches a package evidence/origin value.
        all_origins = {
            tuple(e["origin_um"])
            for d in decisions.values() if d["evidence"]
            for e in d["evidence"]
        } | {
            tuple(d["origin_um"])
            for d in decisions.values() if d["decision"] == merge.RELEASED
        }
        for positions in anchor_tables.values():
            for node in range(total_fragments):
                self.assertIn(
                    (positions[0][node], positions[1][node], positions[2][node]),
                    all_origins,
                )

    def test_batch_cpu_and_memory_budget(self):
        plan, views = self.make_batch()
        plan_path = self.write_plan(plan, name="bench-plan.json")
        bindings = []
        for fragment_id, view in views.items():
            _doc, path = self.write_view(fragment_id, view)
            bindings += ["--view", f"{fragment_id}={path}"]
        out = self.root / "bench-out.json"
        result, (cpu, rss) = measure_cli(
            "merge", "--plan", str(plan_path), "--output", str(out), *bindings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(cpu, 20.0, f"CPU {cpu:.2f}s exceeds 20s budget")
        self.assertLessEqual(rss, 256 * 1024 * 1024,
                             f"RSS {rss / 1024 / 1024:.1f} MiB exceeds 256 MiB")


if __name__ == "__main__":
    unittest.main()
