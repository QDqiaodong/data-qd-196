"""Immutable calibration history, point diagnostics and delivery views.

The upstream state document is one self-contained JSON file.  A version is a
revision of the calibration history; it is never edited in place.  Earlier
versions are recomputed from the stored batches, threshold and change log, so
a later correction cannot rewrite an already published result.

Every numeric input stays the original bounded decimal string.  Derived
coordinates are rendered from ``Fraction`` as an integer or ``n/d`` string;
floating point is never used.
"""

import hashlib
import json
from fractions import Fraction

from .algebra import rank
from .checks import (
    array,
    exact_array,
    fields,
    ident,
    integer,
    number,
    rational,
    unique,
)
from .reconstruct import solve

SCHEMA_VERSION = 1
STORE_KIND = "camera-tracks-state"
DELIVERY_KIND = "camera-tracks-delivery"
MAX_SEQUENCE = 10**9
MAX_VERSIONS = 1_000_000
MAX_BATCHES = 100
MAX_TRACKS_PER_BATCH = 200
MAX_OBSERVATIONS = 20
MAX_CAMERAS = 20
MAX_REPLACEMENTS = 20
MAX_POINTS = MAX_BATCHES * MAX_TRACKS_PER_BATCH
HEX_DIGEST = 64

# Business reasons and their precedence when a point has several problems.
UNOBSERVABLE = "unobservable"
NONPOSITIVE_DEPTH = "nonpositive_depth"
EXCESSIVE_ERROR = "excessive_reprojection_error"
REASON_ORDER = {UNOBSERVABLE: 0, NONPOSITIVE_DEPTH: 1, EXCESSIVE_ERROR: 2}


# ---------------------------------------------------------------------------
# Exact values and canonical encoding


def rational_text(value):
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def canonical_json(value):
    """Compact UTF-8 JSON independent of object key order and whitespace."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _fraction_matrix(rows):
    return tuple(tuple(Fraction(value) for value in row) for row in rows)


def projection_strings(value, where="projection"):
    """Validate a 3x4 matrix of bounded decimal strings; keep the text as-is."""
    rows = exact_array(value, 3, where)
    result = []
    for i, row in enumerate(rows):
        cells = exact_array(row, 4, f"{where}[{i}]")
        for j, cell in enumerate(cells):
            number(cell, f"{where}[{i}][{j}]")
        result.append(list(cells))
    if rank(_fraction_matrix(result)) != 3:
        raise ValueError(f"{where} must have row rank three")
    return result


# ---------------------------------------------------------------------------
# Upstream build input


def validate_build_input(document):
    fields(
        document,
        ("dataset_id", "max_reprojection_error", "cameras", "batches"),
        where="build input",
    )
    dataset_id = ident(document["dataset_id"], "dataset_id")
    threshold_text = document["max_reprojection_error"]
    if number(threshold_text, "max_reprojection_error") < 0:
        raise ValueError("max_reprojection_error must be non-negative")

    cameras = {}
    for index, camera in enumerate(array(document["cameras"], 2, MAX_CAMERAS, "cameras")):
        fields(camera, ("id", "projection"), where=f"camera {index}")
        name = ident(camera["id"], "camera id")
        if name in cameras:
            raise ValueError(f"duplicate camera id: {name}")
        cameras[name] = {"projection": projection_strings(camera["projection"], f"camera {name}")}

    batches = {}
    sequences = []
    for index, batch in enumerate(array(document["batches"], 1, MAX_BATCHES, "batches")):
        fields(batch, ("sequence", "tracks"), where=f"batch {index}")
        sequence = integer(batch["sequence"], 0, MAX_SEQUENCE, f"batch {index} sequence")
        sequences.append(sequence)
        batches[str(sequence)] = {
            "sequence": sequence,
            "tracks": _validate_tracks(batch["tracks"], cameras, f"batch {sequence}"),
        }
    unique(sequences, "batch sequence")

    state = {
        "schema_version": SCHEMA_VERSION,
        "kind": STORE_KIND,
        "dataset_id": dataset_id,
        "version": 0,
        "threshold": threshold_text,
        "cameras": cameras,
        "batches": batches,
        "changes": {},
        "history": [],
        "delivery": {"version": 0, "fingerprint": None, "points": {}},
    }
    version_zero_view(state)
    return state


def _validate_tracks(value, cameras, where):
    tracks = array(value, 1, MAX_TRACKS_PER_BATCH, f"{where} tracks")
    result = []
    ids = []
    for index, track in enumerate(tracks):
        fields(track, ("id", "observations"), where=f"{where} track {index}")
        track_id = ident(track["id"], "track id")
        ids.append(track_id)
        observations = array(
            track["observations"], 2, MAX_OBSERVATIONS, f"track {track_id} observations"
        )
        parsed = []
        used = []
        for obs_index, observation in enumerate(observations):
            fields(
                observation,
                ("camera_id", "xy", "weight"),
                where=f"track {track_id} observation {obs_index}",
            )
            camera_id = ident(observation["camera_id"], "observation camera_id")
            if camera_id not in cameras:
                raise ValueError(f"unknown camera in observation: {camera_id}")
            if camera_id in used:
                raise ValueError(f"duplicate observation camera: {camera_id}")
            used.append(camera_id)
            xy = exact_array(observation["xy"], 2, "observation xy")
            for j, cell in enumerate(xy):
                number(cell, f"observation xy[{j}]")
            if number(observation["weight"], "observation weight") <= 0:
                raise ValueError("positive observation weight required")
            parsed.append(
                {"camera_id": camera_id, "xy": list(xy), "weight": observation["weight"]}
            )
        result.append({"id": track_id, "observations": parsed})
    unique(ids, "track id")
    return result


# ---------------------------------------------------------------------------
# Stored upstream state


def validate_state(document):
    fields(
        document,
        (
            "schema_version",
            "kind",
            "dataset_id",
            "version",
            "threshold",
            "cameras",
            "batches",
            "changes",
            "history",
            "delivery",
        ),
        where="state",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported state schema_version")
    if document["kind"] != STORE_KIND:
        raise ValueError("not a camera-tracks state file")
    ident(document["dataset_id"], "dataset_id")
    version = integer(document["version"], 0, MAX_VERSIONS, "version")
    if number(document["threshold"], "threshold") < 0:
        raise ValueError("state threshold must be non-negative")

    cameras = document["cameras"]
    if not isinstance(cameras, dict) or not 2 <= len(cameras) <= MAX_CAMERAS:
        raise ValueError("state cameras must hold 2..20 entries")
    for name, entry in cameras.items():
        ident(name, "camera id")
        fields(entry, ("projection",), where=f"camera {name}")
        projection_strings(entry["projection"], f"camera {name} projection")

    batches = document["batches"]
    if not isinstance(batches, dict) or not 1 <= len(batches) <= MAX_BATCHES:
        raise ValueError("state batches must hold 1..100 entries")
    sequences = []
    for key, batch in batches.items():
        if not isinstance(key, str) or not key.isdigit() or key != str(int(key)):
            raise ValueError("batch keys must be canonical decimal sequence strings")
        sequence = integer(int(key), 0, MAX_SEQUENCE, "batch sequence")
        sequences.append(sequence)
        fields(batch, ("sequence", "tracks"), where=f"batch {key}")
        if batch["sequence"] != sequence:
            raise ValueError("batch sequence key does not match value")
        _validate_tracks(batch["tracks"], cameras, f"batch {key}")
    unique(sequences, "batch sequence")

    changes = document["changes"]
    if not isinstance(changes, dict):
        raise ValueError("state changes must be an object")
    change_versions = []
    for change_id, entry in changes.items():
        ident(change_id, "change_id")
        fields(entry, ("request", "version", "request_digest"), where=f"change {change_id}")
        change_versions.append(
            integer(entry["version"], 1, MAX_VERSIONS, f"change {change_id} version")
        )
        validate_change_request(entry["request"], cameras, with_change_id=False)
        if entry["request"]["dataset_id"] != document["dataset_id"]:
            raise ValueError("stored change dataset does not match state")
        if entry["request"]["base_version"] != entry["version"] - 1:
            raise ValueError("stored change base_version does not precede its version")
        if entry["request_digest"] != request_digest(entry["request"]):
            raise ValueError("stored change request digest mismatch")
    unique(change_versions, "change version")

    history = document["history"]
    if not isinstance(history, list) or not 1 <= len(history) <= MAX_VERSIONS + 1:
        raise ValueError("state history must be a non-empty array")
    for expected, entry in enumerate(history):
        fields(entry, ("version", "fingerprint", "point_count"), where="history entry")
        integer(entry["version"], 0, MAX_VERSIONS, "history entry version")
        if entry["version"] != expected:
            raise ValueError("history versions must be contiguous from 0")
        _digest(entry["fingerprint"], "history fingerprint")
        integer(entry["point_count"], 0, MAX_POINTS, "history point_count")
    if version != history[-1]["version"]:
        raise ValueError("state version must equal the last history version")
    if set(change_versions) != set(range(1, version + 1)) or len(changes) != version:
        raise ValueError("state versions and stored changes are inconsistent")

    delivery = document["delivery"]
    fields(delivery, ("version", "fingerprint", "points"), where="delivery")
    integer(delivery["version"], 0, MAX_VERSIONS, "delivery version")
    if delivery["version"] != version:
        raise ValueError("delivery view must be materialised at the current version")
    _digest(delivery["fingerprint"], "delivery fingerprint")
    if not isinstance(delivery["points"], dict) or len(delivery["points"]) > MAX_POINTS:
        raise ValueError("delivery points must be an object of at most %d entries" % MAX_POINTS)
    for key, xyz in delivery["points"].items():
        _validate_point_key(key)
        _validate_xyz(xyz, f"delivery point {key}")
    if delivery["fingerprint"] != fingerprint_points(delivery["points"]):
        raise ValueError("delivery fingerprint does not match the stored point table")
    if delivery["fingerprint"] != history[-1]["fingerprint"]:
        raise ValueError("delivery fingerprint disagrees with history")
    if len(delivery["points"]) != history[-1]["point_count"]:
        raise ValueError("delivery point count disagrees with history")
    return document


def _digest(value, where):
    if (
        not isinstance(value, str)
        or len(value) != HEX_DIGEST
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{where} must be a lowercase SHA-256 hex digest")
    return value


def _validate_point_key(key):
    if not isinstance(key, str) or "/" not in key:
        raise ValueError("point keys are <sequence>/<track>")
    sequence_text, track_id = key.split("/", 1)
    if not sequence_text.isdigit() or sequence_text != str(int(sequence_text)):
        raise ValueError("point key sequence must be canonical decimal")
    integer(int(sequence_text), 0, MAX_SEQUENCE, "point sequence")
    ident(track_id, "point track id")


def _validate_xyz(value, where):
    exact_array(value, 3, where)
    for item in value:
        rational(item, where)


def point_key(sequence, track_id):
    return f"{sequence}/{track_id}"


def point_sort_key(key):
    sequence, track_id = key.split("/", 1)
    return (int(sequence), track_id)


# ---------------------------------------------------------------------------
# Change requests


def request_digest(request):
    """SHA-256 of canonical parsed request fields (change_id excluded).

    Object key order and replacement ordering do not participate; matrix
    digits do, exactly as their input strings.
    """
    return hashlib.sha256(canonical_json(request)).hexdigest()


def validate_change_request(document, cameras, *, with_change_id=True):
    """Validate a change request's structure and replacement constraints."""
    if with_change_id:
        fields(
            document,
            ("dataset_id", "change_id", "base_version", "replacements"),
            where="change request",
        )
        ident(document["change_id"], "change_id")
    else:
        fields(
            document,
            ("dataset_id", "base_version", "replacements"),
            where="stored change request",
        )
    ident(document["dataset_id"], "change request dataset_id")
    integer(document["base_version"], 0, MAX_VERSIONS, "base_version")
    replacements = array(document["replacements"], 1, MAX_REPLACEMENTS, "replacements")
    by_camera = {}
    for index, replacement in enumerate(replacements):
        fields(
            replacement,
            ("camera_id", "start", "end", "projection"),
            where=f"replacement {index}",
        )
        camera_id = ident(replacement["camera_id"], "replacement camera_id")
        if camera_id not in cameras:
            raise ValueError(f"unknown camera in replacement: {camera_id}")
        start = integer(replacement["start"], 0, MAX_SEQUENCE, "replacement start")
        end = integer(replacement["end"], 0, MAX_SEQUENCE, "replacement end")
        if start > end:
            raise ValueError("replacement start must not exceed end")
        projection_strings(replacement["projection"], f"replacement {index} projection")
        by_camera.setdefault(camera_id, []).append((start, end))
    for camera_id, intervals in by_camera.items():
        intervals.sort()
        for earlier, later in zip(intervals, intervals[1:]):
            if later[0] <= earlier[1]:
                raise ValueError(
                    f"overlapping replacement intervals for camera {camera_id}"
                )
    return document


def normalize_request(document):
    """Canonical request: replacements sorted, matrix text preserved."""
    replacements = [
        {
            "camera_id": replacement["camera_id"],
            "start": replacement["start"],
            "end": replacement["end"],
            "projection": [list(row) for row in replacement["projection"]],
        }
        for replacement in document["replacements"]
    ]
    replacements.sort(
        key=lambda item: (
            item["camera_id"],
            item["start"],
            item["end"],
            canonical_json(item["projection"]),
        )
    )
    return {
        "dataset_id": document["dataset_id"],
        "base_version": document["base_version"],
        "replacements": replacements,
    }


# ---------------------------------------------------------------------------
# Calibration resolution


def _ordered_changes(state):
    return sorted(state["changes"].items(), key=lambda item: item[1]["version"])


def calibration_at(state, sequence, version):
    """Matrices and source labels for one sequence at one version.

    Later versions win; within a version one camera's intervals are disjoint.
    Only changes at or before ``version`` are visible, so a historical query
    never sees newer calibration.
    """
    matrices = {
        name: _fraction_matrix(entry["projection"])
        for name, entry in state["cameras"].items()
    }
    sources = {
        name: {"source": "initial", "change_id": None, "version": None}
        for name in state["cameras"]
    }
    for change_id, entry in _ordered_changes(state):
        if entry["version"] > version:
            continue
        for replacement in entry["request"]["replacements"]:
            if replacement["start"] <= sequence <= replacement["end"]:
                camera_id = replacement["camera_id"]
                matrices[camera_id] = _fraction_matrix(replacement["projection"])
                sources[camera_id] = {
                    "source": "change",
                    "change_id": change_id,
                    "version": entry["version"],
                }
    return matrices, sources


# ---------------------------------------------------------------------------
# Per-point diagnostics


def evaluate_track(track, sequence, matrices, threshold_squared, sources):
    solved = solve(track, matrices)
    observations = []
    reasons = []
    if solved["status"] == "unobservable":
        reasons.append(UNOBSERVABLE)
    else:
        depth_failure = False
        error_failure = False
        for diagnostic in solved["observations"]:
            depth = diagnostic["depth"]
            squared = diagnostic["squared_residual"]
            if depth <= 0:
                depth_failure = True
            if squared is not None and squared > threshold_squared:
                error_failure = True
            observations.append(
                {
                    "camera_id": diagnostic["camera_id"],
                    "source": sources[diagnostic["camera_id"]],
                    "depth": rational_text(depth),
                    "positive_depth": diagnostic["positive_depth"],
                    "projected_xy": None
                    if diagnostic["projected_xy"] is None
                    else [rational_text(v) for v in diagnostic["projected_xy"]],
                    "residual_xy": None
                    if diagnostic["residual_xy"] is None
                    else [rational_text(v) for v in diagnostic["residual_xy"]],
                    "squared_residual": None
                    if squared is None
                    else rational_text(squared),
                }
            )
        if depth_failure:
            reasons.append(NONPOSITIVE_DEPTH)
        if error_failure:
            reasons.append(EXCESSIVE_ERROR)
    reasons.sort(key=lambda reason: REASON_ORDER[reason])
    xyz = None if solved["xyz"] is None else [rational_text(v) for v in solved["xyz"]]
    return {
        "sequence": sequence,
        "track_id": track["id"],
        "calibration_sources": {
            observation["camera_id"]: sources[observation["camera_id"]]
            for observation in track["observations"]
        },
        "status": solved["status"],
        "xyz": xyz,
        "observations": observations,
        "algebraic_squared_error": None
        if solved["algebraic_squared_error"] is None
        else rational_text(solved["algebraic_squared_error"]),
        "deliverable": not reasons,
        "reason": None if not reasons else reasons[0],
        "reasons": reasons,
    }


def _find_track(state, sequence, track_id):
    batch = state["batches"].get(str(sequence))
    if batch is None:
        raise ValueError(f"unknown acquisition sequence: {sequence}")
    track = next((t for t in batch["tracks"] if t["id"] == track_id), None)
    if track is None:
        raise ValueError(f"unknown track id in sequence {sequence}: {track_id}")
    return track


def query_point(state, version, sequence, track_id):
    integer(version, 0, MAX_VERSIONS, "version")
    if version > state["version"]:
        raise ValueError(f"version {version} has not been created")
    integer(sequence, 0, MAX_SEQUENCE, "sequence")
    ident(track_id, "track_id")
    track = _find_track(state, sequence, track_id)
    matrices, sources = calibration_at(state, sequence, version)
    record = evaluate_track(
        track, sequence, matrices, number(state["threshold"]) ** 2, sources
    )
    record["dataset_id"] = state["dataset_id"]
    record["version"] = version
    return record


# ---------------------------------------------------------------------------
# Delivery views and fingerprints


def compute_view(state, version):
    """Materialise the deliverable point table at one version.

    Callers that expose a version publicly must first establish that it has
    been created; this function also supports computing the next version
    inside the correction transaction before it is committed.
    """
    integer(version, 0, MAX_VERSIONS, "version")
    threshold_squared = number(state["threshold"]) ** 2
    points = {}
    for sequence_text in sorted(state["batches"], key=int):
        sequence = int(sequence_text)
        matrices, sources = calibration_at(state, sequence, version)
        for track in state["batches"][sequence_text]["tracks"]:
            record = evaluate_track(track, sequence, matrices, threshold_squared, sources)
            if record["deliverable"]:
                points[point_key(sequence, track["id"])] = record["xyz"]
    return points


def _history_entry(state, version):
    for entry in state["history"]:
        if entry["version"] == version:
            return entry
    raise ValueError(f"version {version} has not been created")


def view_at(state, version):
    integer(version, 0, MAX_VERSIONS, "version")
    if version > state["version"]:
        raise ValueError(f"version {version} has not been created")
    if version == state["version"]:
        return {key: list(value) for key, value in state["delivery"]["points"].items()}
    return compute_view(state, version)


def fingerprint_at(state, version):
    entry = _history_entry(state, version)
    if version == state["version"]:
        return entry["fingerprint"]
    actual = fingerprint_points(compute_view(state, version))
    if actual != entry["fingerprint"]:
        raise ValueError(f"history fingerprint for version {version} is inconsistent")
    return entry["fingerprint"]


def version_zero_view(state):
    """Compute and record version 0's view and fingerprint in the state."""
    points = compute_view(state, 0)
    fingerprint = fingerprint_points(points)
    state["history"] = [
        {"version": 0, "fingerprint": fingerprint, "point_count": len(points)},
    ]
    state["delivery"] = {"version": 0, "fingerprint": fingerprint, "points": points}
    return points


def record_version(state, version, points):
    state["version"] = version
    state["history"].append(
        {"version": version, "fingerprint": fingerprint_points(points), "point_count": len(points)}
    )
    state["delivery"] = {
        "version": version,
        "fingerprint": state["history"][-1]["fingerprint"],
        "points": points,
    }


def fingerprint_points(points):
    """SHA-256 over a canonical point table.

    UTF-8, LF line endings::

        camera-tracks-points-v1
        count=<number of points>
        <sequence>\\t<track id>\\t<x>\\t<y>\\t<z>

    One line per point, sorted by numeric sequence then track id (ASCII
    code-point order), coordinates as exact rational text.  The header and
    count make empty views and prefix collisions distinguishable.  No JSON
    syntax participates, so the digest is independent of whitespace and
    object field order.
    """
    lines = ["camera-tracks-points-v1", f"count={len(points)}"]
    for key in sorted(points, key=point_sort_key):
        sequence, track_id = key.split("/", 1)
        lines.append(f"{sequence}\t{track_id}\t" + "\t".join(points[key]))
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def diff_views(previous, current):
    records = []
    for key in sorted(set(previous) | set(current), key=point_sort_key):
        sequence_text, track_id = key.split("/", 1)
        if key in current and previous.get(key) != current[key]:
            records.append(
                {
                    "op": "upsert",
                    "sequence": int(sequence_text),
                    "track_id": track_id,
                    "xyz": list(current[key]),
                }
            )
        elif key not in current:
            records.append(
                {"op": "delete", "sequence": int(sequence_text), "track_id": track_id}
            )
    return records


def build_full_delivery(state):
    # Version 0 is immutable; use the materialised table when it is current
    # and recompute from the batch log otherwise, then cross-check history.
    points = view_at(state, 0)
    expected = _history_entry(state, 0)["fingerprint"]
    actual = fingerprint_points(points)
    if actual != expected:
        raise ValueError("version-0 view no longer matches its recorded fingerprint")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DELIVERY_KIND,
        "dataset_id": state["dataset_id"],
        "base_version": None,
        "target_version": 0,
        "base_fingerprint": None,
        "target_fingerprint": fingerprint_at(state, 0),
        "records": diff_views({}, points),
    }


def build_delta_delivery(state, base_version):
    integer(base_version, 0, MAX_VERSIONS, "base_version")
    target = base_version + 1
    if target > state["version"]:
        raise ValueError(
            f"cannot export versions {base_version}->{target}; "
            f"current version is {state['version']}"
        )
    previous = view_at(state, base_version)
    current = view_at(state, target)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DELIVERY_KIND,
        "dataset_id": state["dataset_id"],
        "base_version": base_version,
        "target_version": target,
        "base_fingerprint": fingerprint_at(state, base_version),
        "target_fingerprint": fingerprint_at(state, target),
        "records": diff_views(previous, current),
    }


def validate_delivery(document):
    fields(
        document,
        (
            "schema_version",
            "kind",
            "dataset_id",
            "base_version",
            "target_version",
            "base_fingerprint",
            "target_fingerprint",
            "records",
        ),
        where="delivery",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported delivery schema_version")
    if document["kind"] != DELIVERY_KIND:
        raise ValueError("not a camera-tracks delivery file")
    ident(document["dataset_id"], "delivery dataset_id")
    target_version = integer(document["target_version"], 0, MAX_VERSIONS, "target_version")
    if document["base_version"] is None:
        if target_version != 0:
            raise ValueError("a full delivery targets version 0")
        if document["base_fingerprint"] is not None:
            raise ValueError("full delivery base_fingerprint must be null")
    else:
        base_version = integer(document["base_version"], 0, MAX_VERSIONS, "base_version")
        if target_version != base_version + 1:
            raise ValueError("a delivery must span exactly one next version")
        _digest(document["base_fingerprint"], "base_fingerprint")
    _digest(document["target_fingerprint"], "target_fingerprint")

    records = array(document["records"], 0, MAX_POINTS, "records")
    identities = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"record {index} must be an object")
        operation = record.get("op")
        if operation == "upsert":
            fields(record, ("op", "sequence", "track_id", "xyz"), where=f"record {index}")
            _validate_xyz(record["xyz"], f"record {index} xyz")
        elif operation == "delete":
            fields(record, ("op", "sequence", "track_id"), where=f"record {index}")
        else:
            raise ValueError(f"record {index} op must be upsert or delete")
        sequence = integer(record["sequence"], 0, MAX_SEQUENCE, f"record {index} sequence")
        track_id = ident(record["track_id"], f"record {index} track_id")
        identities.append((sequence, track_id))
    unique(identities, "record identity")
    if identities != sorted(identities):
        raise ValueError("records must be sorted by sequence then track id")
    return document


def decode_delivery(raw):
    from .io import decode

    return validate_delivery(decode(raw))


def apply_delivery(base_points, document):
    """Validate one package against a point table and return the new table."""
    table = {key: list(value) for key, value in base_points.items()}
    for index, record in enumerate(document["records"]):
        key = point_key(record["sequence"], record["track_id"])
        if record["op"] == "upsert":
            table[key] = list(record["xyz"])
        else:
            if key not in table:
                raise ValueError(f"delete of unknown point at record {index}: {key}")
            del table[key]
    actual = fingerprint_points(table)
    if actual != document["target_fingerprint"]:
        raise ValueError(
            "applied point table does not match target_fingerprint "
            f"(expected {document['target_fingerprint']}, got {actual})"
        )
    return table
