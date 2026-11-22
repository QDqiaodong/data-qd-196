"""Offline resurvey displacement acceptance (复测位移验收).

The ``accept`` subcommand compares two delivered point tables — a baseline
survey and a resurvey — inside one acceptance zone.  Point identities may
have been relabelled between the two surveys, so the zone configuration
supplies field candidates plus a few manually confirmed pairings; the
command never treats same-named or nearest points as identical.

A complete correspondence covers every required point on both sides exactly
once, uses only candidate pairs, and honours every confirmation.  When none
exists the report names a Hall-deficient set of baseline points.  Otherwise
it lists, per baseline point, every resurvey point it can be paired with in
some complete correspondence, and the attainable minimum and maximum numbers
of points whose displacement exceeds the limit.  Displacements use exact
rational coordinates; a displacement equal to the limit is not exceeded.

The command only reads its inputs: it never writes to the point tables and
never fits coordinates.  The report is created as one new file; an existing
report is never overwritten, and invalid input produces no report at all.
"""

from fractions import Fraction
from pathlib import Path

from . import io
from .checks import array, exact_array, fields, ident, integer, number, rational
from .matching import (
    allowed_partners,
    alternating_reach,
    hopcroft_karp,
    min_cost_matching,
)
from .state import (
    MAX_POINTS,
    MAX_SEQUENCE,
    MAX_VERSIONS,
    SCHEMA_VERSION,
    _digest,
    fingerprint_points,
    point_key,
)

VIEW_KIND = "camera-tracks-view"
CONFIG_KIND = "camera-tracks-acceptance"
REPORT_KIND = "camera-tracks-acceptance-report"
MAX_REQUIRED = 4096
MAX_CANDIDATES = 32768

SIGN = "sign"
FAIL = "fail"
VERIFY_LABELS = "verify_labels"
INCOMPLETE = "incomplete_correspondence"


# ---------------------------------------------------------------------------
# Zone configuration


def _endpoint(value, where):
    fields(value, ("dataset_id", "version", "fingerprint"), where=where)
    ident(value["dataset_id"], f"{where} dataset_id")
    integer(value["version"], 0, MAX_VERSIONS, f"{where} version")
    _digest(value["fingerprint"], f"{where} fingerprint")
    return {
        "dataset_id": value["dataset_id"],
        "version": value["version"],
        "fingerprint": value["fingerprint"],
    }


def _point_ref(value, where):
    fields(value, ("sequence", "track_id"), where=where)
    sequence = integer(value["sequence"], 0, MAX_SEQUENCE, f"{where} sequence")
    track_id = ident(value["track_id"], f"{where} track_id")
    return (sequence, track_id)


def _required(value, where):
    entries = array(value, 1, MAX_REQUIRED, where)
    refs = [_point_ref(entry, f"{where} {index}") for index, entry in enumerate(entries)]
    if len(set(refs)) != len(refs):
        raise ValueError(f"duplicate {where} identity")
    return sorted(refs)


def _limit(value):
    if not isinstance(value, str):
        raise ValueError("displacement_limit must be a non-negative rational string")
    try:
        rational(value, "displacement_limit")
    except ValueError:
        # A bounded decimal string is exact too and equally accepted.
        number(value, "displacement_limit")
    limit = Fraction(value)
    if limit < 0:
        raise ValueError("displacement_limit must be non-negative")
    return limit


def _pair_ref(value, index, role):
    where = f"{role} {index}"
    fields(value, ("baseline", "resurvey"), where=where)
    baseline = _point_ref(value["baseline"], f"{where} baseline")
    resurvey = _point_ref(value["resurvey"], f"{where} resurvey")
    return baseline, resurvey


def load_config(path):
    document = io.load(path)
    fields(
        document,
        (
            "schema_version",
            "kind",
            "zone_id",
            "displacement_limit",
            "baseline",
            "resurvey",
            "baseline_required",
            "resurvey_required",
            "candidates",
            "confirmations",
        ),
        where="acceptance config",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported acceptance config schema_version")
    if document["kind"] != CONFIG_KIND:
        raise ValueError("not a camera-tracks acceptance config")
    zone_id = ident(document["zone_id"], "zone_id")
    limit = _limit(document["displacement_limit"])
    baseline_ep = _endpoint(document["baseline"], "config baseline")
    resurvey_ep = _endpoint(document["resurvey"], "config resurvey")
    baseline_required = _required(document["baseline_required"], "baseline_required")
    resurvey_required = _required(document["resurvey_required"], "resurvey_required")
    if len(baseline_required) != len(resurvey_required):
        raise ValueError(
            "baseline_required and resurvey_required must have the same size"
        )

    baseline_index = {ref: i for i, ref in enumerate(baseline_required)}
    resurvey_index = {ref: i for i, ref in enumerate(resurvey_required)}

    def locate(pair, index, role):
        baseline_ref, resurvey_ref = _pair_ref(pair, index, role)
        if baseline_ref not in baseline_index:
            raise ValueError(
                f"{role} {index} baseline endpoint {baseline_ref!r} "
                "is not in the baseline required list"
            )
        if resurvey_ref not in resurvey_index:
            raise ValueError(
                f"{role} {index} resurvey endpoint {resurvey_ref!r} "
                "is not in the resurvey required list"
            )
        return baseline_index[baseline_ref], resurvey_index[resurvey_ref]

    entries = array(document["candidates"], 0, MAX_CANDIDATES, "candidates")
    candidates = [locate(pair, i, "candidate") for i, pair in enumerate(entries)]
    if len(set(candidates)) != len(candidates):
        raise ValueError("duplicate candidate")
    candidates.sort()
    candidate_set = set(candidates)

    entries = array(document["confirmations"], 0, MAX_CANDIDATES, "confirmations")
    confirmations = []
    used_a = set()
    used_b = set()
    for i, pair in enumerate(entries):
        edge = locate(pair, i, "confirmation")
        if edge not in candidate_set:
            raise ValueError(f"confirmation {i} is not among the candidates")
        if edge[0] in used_a:
            raise ValueError("confirmations share a baseline endpoint")
        if edge[1] in used_b:
            raise ValueError("confirmations share a resurvey endpoint")
        used_a.add(edge[0])
        used_b.add(edge[1])
        confirmations.append(edge)
    confirmations.sort()

    return {
        "zone_id": zone_id,
        "limit": limit,
        "limit_text": document["displacement_limit"],
        "baseline": baseline_ep,
        "resurvey": resurvey_ep,
        "baseline_required": baseline_required,
        "resurvey_required": resurvey_required,
        "candidates": candidates,
        "confirmations": confirmations,
    }


# ---------------------------------------------------------------------------
# Delivered point tables (view exports)


def load_view(path, endpoint, role):
    document = io.load(path, io.MAX_STORE_BYTES)
    fields(
        document,
        ("schema_version", "kind", "dataset_id", "version", "fingerprint", "points"),
        where=f"{role} view",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported {role} view schema_version")
    if document["kind"] != VIEW_KIND:
        raise ValueError(f"{role} file is not a camera-tracks view")
    ident(document["dataset_id"], f"{role} view dataset_id")
    integer(document["version"], 0, MAX_VERSIONS, f"{role} view version")
    _digest(document["fingerprint"], f"{role} view fingerprint")
    points = {}
    for index, entry in enumerate(
        array(document["points"], 0, MAX_POINTS, f"{role} view points")
    ):
        fields(entry, ("sequence", "track_id", "xyz"), where=f"{role} view point {index}")
        sequence = integer(
            entry["sequence"], 0, MAX_SEQUENCE, f"{role} view point {index} sequence"
        )
        track_id = ident(entry["track_id"], f"{role} view point {index} track_id")
        exact_array(entry["xyz"], 3, f"{role} view point {index} xyz")
        for cell in entry["xyz"]:
            rational(cell, f"{role} view point {index} xyz")
        key = point_key(sequence, track_id)
        if key in points:
            raise ValueError(f"duplicate point in {role} view: {key}")
        points[key] = list(entry["xyz"])
    if fingerprint_points(points) != document["fingerprint"]:
        raise ValueError(f"{role} view fingerprint does not match its point table")
    for field in ("dataset_id", "version", "fingerprint"):
        if document[field] != endpoint[field]:
            raise ValueError(
                f"{role} view {field} {document[field]!r} does not match "
                f"the configured {endpoint[field]!r}"
            )
    return points


# ---------------------------------------------------------------------------
# Evaluation


def _ref_json(ref):
    return {"sequence": ref[0], "track_id": ref[1]}


def build_report(config, baseline_points, resurvey_points):
    baseline_refs = config["baseline_required"]
    resurvey_refs = config["resurvey_required"]
    n = len(baseline_refs)
    for ref in baseline_refs:
        if point_key(*ref) not in baseline_points:
            raise ValueError(f"required point {ref!r} is not present in the baseline view")
    for ref in resurvey_refs:
        if point_key(*ref) not in resurvey_points:
            raise ValueError(f"required point {ref!r} is not present in the resurvey view")

    baseline_xyz = [
        tuple(Fraction(cell) for cell in baseline_points[point_key(*ref)])
        for ref in baseline_refs
    ]
    resurvey_xyz = [
        tuple(Fraction(cell) for cell in resurvey_points[point_key(*ref)])
        for ref in resurvey_refs
    ]
    limit_squared = config["limit"] ** 2
    exceeded = {}
    for a, b in config["candidates"]:
        squared = sum(
            (x - y) ** 2 for x, y in zip(baseline_xyz[a], resurvey_xyz[b])
        )
        exceeded[(a, b)] = squared > limit_squared

    confirmations = config["confirmations"]
    confirmed_a = {a for a, _ in confirmations}
    confirmed_b = {b for _, b in confirmations}
    confirmed_partner = dict(confirmations)
    confirmed_exceeded = sum(1 for edge in confirmations if exceeded[edge])

    remaining_a = [a for a in range(n) if a not in confirmed_a]
    remaining_b = [b for b in range(n) if b not in confirmed_b]
    local_a = {a: i for i, a in enumerate(remaining_a)}
    local_b = {b: i for i, b in enumerate(remaining_b)}
    n_local = len(remaining_a)
    adjacency = [[] for _ in range(n_local)]
    for a, b in config["candidates"]:
        if a in local_a and b in local_b:
            adjacency[local_a[a]].append(local_b[b])

    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "zone_id": config["zone_id"],
        "displacement_limit": config["limit_text"],
        "baseline": config["baseline"],
        "resurvey": config["resurvey"],
        "required_points": n,
        "candidates": len(config["candidates"]),
        "confirmations": len(confirmations),
    }

    def correspondence(match):
        pairs = list(confirmations)
        if match is not None:
            pairs += [
                (remaining_a[i], remaining_b[j]) for i, j in enumerate(match)
            ]
        pairs.sort()
        entries = [
            {
                "baseline": _ref_json(baseline_refs[a]),
                "resurvey": _ref_json(resurvey_refs[b]),
                "exceeded": exceeded[(a, b)],
            }
            for a, b in pairs
        ]
        return entries, sum(1 for entry in entries if entry["exceeded"])

    if n_local:
        match_a, match_b = hopcroft_karp(adjacency, n_local)
        complete = all(m != -1 for m in match_a)
    else:
        match_a, match_b, complete = [], [], True

    if not complete:
        seen_a, seen_b = alternating_reach(adjacency, match_a, match_b)
        deficient = [remaining_a[i] for i, seen in enumerate(seen_a) if seen]
        neighbours = [remaining_b[j] for j, seen in enumerate(seen_b) if seen]
        if not deficient or len(neighbours) >= len(deficient):
            raise RuntimeError("internal error: invalid Hall-deficient witness")
        report["complete"] = False
        report["conclusion"] = INCOMPLETE
        report["deficient"] = {
            "baseline_points": [_ref_json(baseline_refs[a]) for a in deficient],
            "available_resurvey_points": [
                _ref_json(resurvey_refs[b]) for b in neighbours
            ],
        }
        return report, {"deficient_points": len(deficient)}

    # Per-point partners across all complete correspondences.
    if n_local:
        allowed = allowed_partners(adjacency, match_a, match_b)
    points_report = []
    for a in range(n):
        if a in confirmed_a:
            partners = [confirmed_partner[a]]
        else:
            partners = [remaining_b[j] for j in allowed[local_a[a]]]
        entries = [
            {
                **_ref_json(resurvey_refs[b]),
                "exceeded": exceeded[(a, b)],
            }
            for b in partners
        ]
        flags = {entry["exceeded"] for entry in entries}
        if len(flags) == 2:
            verdict = "depends"
        elif flags == {True}:
            verdict = "always_exceeded"
        else:
            verdict = "never_exceeded"
        points_report.append(
            {
                **_ref_json(baseline_refs[a]),
                "verdict": verdict,
                "partners": entries,
            }
        )

    # Attainable min/max exceeded counts, each with one witness.
    if n_local:
        costs = [
            [1 if exceeded[(remaining_a[i], remaining_b[j])] else 0 for j in row]
            for i, row in enumerate(adjacency)
        ]
        min_result = min_cost_matching(adjacency, costs, n_local)
        flipped = [[1 - cost for cost in row] for row in costs]
        max_result = min_cost_matching(adjacency, flipped, n_local)
        if min_result is None or max_result is None:
            raise RuntimeError("internal error: complete correspondence lost")
        min_entries, min_count = correspondence(min_result[1])
        max_entries, max_count = correspondence(max_result[1])
        if min_count != min_result[0] + confirmed_exceeded:
            raise RuntimeError("internal error: minimum witness count mismatch")
        if max_count != n_local - max_result[0] + confirmed_exceeded:
            raise RuntimeError("internal error: maximum witness count mismatch")
    else:
        min_entries, min_count = correspondence([])
        max_entries, max_count = min_entries, min_count

    if max_count == 0:
        conclusion = SIGN
    elif min_count > 0:
        conclusion = FAIL
    else:
        conclusion = VERIFY_LABELS
    report["complete"] = True
    report["conclusion"] = conclusion
    report["points"] = points_report
    report["exceeded"] = {
        "min": {"count": min_count, "correspondence": min_entries},
        "max": {"count": max_count, "correspondence": max_entries},
    }
    return report, {"min_exceeded": min_count, "max_exceeded": max_count}


# ---------------------------------------------------------------------------
# Command entry


def run(baseline_path, resurvey_path, config_path, output_path):
    output = Path(output_path)
    inputs = {Path(p).resolve() for p in (baseline_path, resurvey_path, config_path)}
    if output.resolve() in inputs:
        raise ValueError("output path must differ from the input files")
    if output.exists() or output.is_symlink():
        raise ValueError(f"report already exists: {output_path}")
    config = load_config(config_path)
    baseline_points = load_view(baseline_path, config["baseline"], "baseline")
    resurvey_points = load_view(resurvey_path, config["resurvey"], "resurvey")
    report, extra = build_report(config, baseline_points, resurvey_points)
    io.publish_new(output, io.dump_store(report))
    summary = {
        "zone_id": config["zone_id"],
        "complete": report["complete"],
        "conclusion": report["conclusion"],
        "output": str(output_path),
    }
    summary.update(extra)
    return summary
