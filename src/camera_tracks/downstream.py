"""Downstream commands: apply one delivery package, query the local view.

The downstream store is a separate local JSON file with no shared database.
It holds the currently visible point table plus the exact byte digests of
packages already applied.  A package is applied in one atomic file
replacement; validation always completes before that replacement, so a
rejected or interrupted application changes nothing.
"""

import hashlib
from pathlib import Path

from . import io
from .faults import APPLY_AFTER_COMMIT, APPLY_BEFORE_COMMIT, checkpoint
from .checks import exact_array, fields, ident, integer, rational
from .state import (
    SCHEMA_VERSION,
    _digest,
    _validate_point_key,
    apply_delivery,
    decode_delivery,
    fingerprint_points,
    point_key,
    point_sort_key,
)

DOWNSTREAM_KIND = "camera-tracks-downstream"
MAX_DOWNSTREAM_VERSION = 10**9


def empty_downstream():
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DOWNSTREAM_KIND,
        "dataset_id": None,
        "version": None,
        "fingerprint": None,
        "points": {},
        "packages": [],
    }


def validate_downstream(document):
    fields(
        document,
        (
            "schema_version",
            "kind",
            "dataset_id",
            "version",
            "fingerprint",
            "points",
            "packages",
        ),
        where="downstream store",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported downstream schema_version")
    if document["kind"] != DOWNSTREAM_KIND:
        raise ValueError("not a camera-tracks downstream store")
    version = document["version"]
    if version is None:
        if (
            document["dataset_id"] is not None
            or document["fingerprint"] is not None
            or document["points"]
            or document["packages"]
        ):
            raise ValueError("an empty downstream store has no dataset, points or packages")
    else:
        integer(version, 0, MAX_DOWNSTREAM_VERSION, "downstream version")
        ident(document["dataset_id"], "downstream dataset_id")
        _digest(document["fingerprint"], "downstream fingerprint")
        if not isinstance(document["points"], dict):
            raise ValueError("downstream points must be an object")
        for key, value in document["points"].items():
            _validate_point_key(key)
            exact_array(value, 3, f"downstream point {key}")
            for item in value:
                rational(item, f"downstream point {key}")
        if document["fingerprint"] != fingerprint_points(document["points"]):
            raise ValueError("downstream fingerprint does not match its point table")
    if not isinstance(document["packages"], list):
        raise ValueError("downstream packages must be an array")
    seen_targets = set()
    for entry in document["packages"]:
        fields(
            entry,
            ("target_version", "base_version", "package_digest", "target_fingerprint"),
            where="applied package",
        )
        target = integer(entry["target_version"], 0, MAX_DOWNSTREAM_VERSION,
                         "applied target_version")
        if entry["base_version"] is not None:
            integer(entry["base_version"], 0, MAX_DOWNSTREAM_VERSION, "applied base_version")
        _digest(entry["package_digest"], "applied package_digest")
        _digest(entry["target_fingerprint"], "applied target_fingerprint")
        if target in seen_targets:
            raise ValueError("two applied packages claim the same target version")
        seen_targets.add(target)
    return document


def load_downstream(path):
    if not Path(path).exists() and not Path(path).is_symlink():
        return empty_downstream()
    return validate_downstream(io.decode(io.read_bytes(path)))


def write_downstream(path, state):
    io.replace_atomic(path, io.dump_store(state))


def _package_record(document, digest):
    return {
        "target_version": document["target_version"],
        "base_version": document["base_version"],
        "package_digest": digest,
        "target_fingerprint": document["target_fingerprint"],
    }


def _render_points(points):
    return [
        {
            "sequence": int(key.split("/", 1)[0]),
            "track_id": key.split("/", 1)[1],
            "xyz": list(points[key]),
        }
        for key in sorted(points, key=point_sort_key)
    ]


def apply(store_path, package_path):
    state = load_downstream(store_path)
    raw = io.read_bytes(package_path)
    digest = hashlib.sha256(raw).hexdigest()

    # Exact original bytes are recognised first: a retried package answers
    # with its original success version and never rewrites the local table.
    for entry in state["packages"]:
        if entry["package_digest"] == digest:
            return {
                "dataset_id": state["dataset_id"],
                "status": "already_applied",
                "target_version": entry["target_version"],
                "version": state["version"],
                "fingerprint": state["fingerprint"],
                "points": len(state["points"]),
            }

    document = decode_delivery(raw)

    if state["version"] is None:
        if document["base_version"] is not None:
            raise ValueError("empty downstream store accepts only the first full delivery")
        points = apply_delivery({}, document)
        version = 0
        state["dataset_id"] = document["dataset_id"]
    else:
        if document["dataset_id"] != state["dataset_id"]:
            raise ValueError(
                f"delivery dataset {document['dataset_id']!r} does not match "
                f"downstream dataset {state['dataset_id']!r}"
            )
        if document["target_version"] <= state["version"]:
            raise ValueError(
                f"delivery targets version {document['target_version']}, not after "
                f"current version {state['version']}; its bytes differ from the "
                "originally applied package"
            )
        if document["target_version"] != state["version"] + 1:
            raise ValueError(
                f"delivery skips versions: base {document['base_version']}, "
                f"target {document['target_version']}, current {state['version']}"
            )
        if document["base_version"] != state["version"]:
            raise ValueError(
                f"delivery base version {document['base_version']} does not match "
                f"current version {state['version']}"
            )
        if document["base_fingerprint"] != state["fingerprint"]:
            raise ValueError(
                "delivery base_fingerprint does not match the current downstream view"
            )
        points = apply_delivery(state["points"], document)
        version = document["target_version"]

    updated = {
        "schema_version": SCHEMA_VERSION,
        "kind": DOWNSTREAM_KIND,
        "dataset_id": state["dataset_id"],
        "version": version,
        "fingerprint": document["target_fingerprint"],
        "points": points,
        "packages": state["packages"] + [_package_record(document, digest)],
    }
    validate_downstream(updated)
    checkpoint(APPLY_BEFORE_COMMIT)
    write_downstream(store_path, updated)
    checkpoint(APPLY_AFTER_COMMIT)
    return {
        "dataset_id": updated["dataset_id"],
        "status": "applied",
        "target_version": version,
        "version": version,
        "fingerprint": updated["fingerprint"],
        "points": len(points),
    }


def query(store_path, sequence, track_id):
    state = load_downstream(store_path)
    key = point_key(sequence, track_id)
    found = key in state["points"]
    return {
        "dataset_id": state["dataset_id"],
        "version": state["version"],
        "fingerprint": state["fingerprint"],
        "sequence": sequence,
        "track_id": track_id,
        "found": found,
        "xyz": list(state["points"][key]) if found else None,
    }


def export_view(store_path, output_path):
    if output_path is not None and Path(store_path).resolve() == Path(output_path).resolve():
        raise ValueError("output path must differ from the state store")
    state = load_downstream(store_path)
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": "camera-tracks-view",
        "dataset_id": state["dataset_id"],
        "version": state["version"],
        "fingerprint": state["fingerprint"],
        "points": _render_points(state["points"]),
    }
    if output_path is None:
        return document
    io.replace_atomic(output_path, io.dump_store(document))
    return {
        "dataset_id": document["dataset_id"],
        "version": document["version"],
        "fingerprint": document["fingerprint"],
        "points": len(document["points"]),
        "output": str(output_path),
    }
