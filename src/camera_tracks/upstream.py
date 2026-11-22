"""Upstream commands: build the store, revise calibration, query, export."""

from pathlib import Path

from . import io
from .faults import (
    BUILD_BEFORE_COMMIT,
    CORRECT_AFTER_COMMIT,
    CORRECT_BEFORE_COMMIT,
    EXPORT_BEFORE_WRITE,
    checkpoint,
)
from .state import (
    build_delta_delivery,
    build_full_delivery,
    canonical_json,
    compute_view,
    normalize_request,
    query_point,
    record_version,
    request_digest,
    validate_build_input,
    validate_change_request,
    validate_state,
)


def load_state(path):
    raw = io.read_bytes(path)
    document = io.decode(raw)
    return validate_state(document)


def write_state(path, state):
    io.replace_atomic(path, io.dump_store(state))


# A build may carry 100 batches of 200 tracks with 20 observations each,
# which is far larger than one legacy single-dataset report input.
MAX_BUILD_BYTES = 64 * 1024 * 1024


def build(input_path, store_path):
    document = io.load(input_path, MAX_BUILD_BYTES)
    state = validate_build_input(document)
    checkpoint(BUILD_BEFORE_COMMIT)
    # A build never overwrites an existing state file.
    if Path(store_path).exists() or Path(store_path).is_symlink():
        raise ValueError(f"store already exists: {store_path}")
    io.publish_new(store_path, io.dump_store(state))
    return {
        "dataset_id": state["dataset_id"],
        "version": 0,
        "fingerprint": state["delivery"]["fingerprint"],
        "points": len(state["delivery"]["points"]),
        "store": str(store_path),
    }


def correct(store_path, request_path):
    state = load_state(store_path)
    document = io.load(request_path)
    # Structure is checked before any idempotency decision so a malformed
    # retry can never be mistaken for a previously accepted request.
    validate_change_request(document, state["cameras"])
    if document["dataset_id"] != state["dataset_id"]:
        raise ValueError(
            f"change dataset_id {document['dataset_id']!r} does not match "
            f"state {state['dataset_id']!r}"
        )
    normalized = normalize_request(document)
    digest = request_digest(normalized)
    change_id = document["change_id"]

    existing = state["changes"].get(change_id)
    if existing is not None:
        if existing["request_digest"] != digest:
            raise ValueError(
                f"change_id {change_id!r} was already used for a different request"
            )
        return {
            "dataset_id": state["dataset_id"],
            "change_id": change_id,
            "status": "already_applied",
            "version": existing["version"],
            "current_version": state["version"],
            "fingerprint": state["delivery"]["fingerprint"],
        }

    # A previously successful id takes precedence over the stale-base check;
    # a new id still has to name the current base version.
    if document["base_version"] != state["version"]:
        raise ValueError(
            f"base_version {document['base_version']} is stale; "
            f"current version is {state['version']}"
        )

    version = state["version"] + 1
    state["changes"][change_id] = {
        "request": normalized,
        "version": version,
        "request_digest": digest,
    }
    points = compute_view(state, version)
    record_version(state, version, points)

    checkpoint(CORRECT_BEFORE_COMMIT)
    write_state(store_path, state)
    checkpoint(CORRECT_AFTER_COMMIT)
    return {
        "dataset_id": state["dataset_id"],
        "change_id": change_id,
        "status": "applied",
        "version": version,
        "previous_version": version - 1,
        "fingerprint": state["delivery"]["fingerprint"],
        "points": len(points),
    }


def query(store_path, version, sequence, track_id):
    state = load_state(store_path)
    return query_point(state, version, sequence, track_id)


def _guard_distinct(store_path, output_path):
    store = Path(store_path).resolve()
    output = Path(output_path).resolve()
    if store == output:
        raise ValueError("output path must differ from the state store")


def export(store_path, output_path, full=False, base_version=None):
    _guard_distinct(store_path, output_path)
    state = load_state(store_path)
    if full:
        if base_version is not None:
            raise ValueError("choose either --full or --base-version, not both")
        document = build_full_delivery(state)
    else:
        if base_version is None:
            raise ValueError("export needs --full or --base-version")
        document = build_delta_delivery(state, base_version)
    payload = canonical_json(document)
    checkpoint(EXPORT_BEFORE_WRITE)
    io.replace_atomic(output_path, payload)
    return {
        "dataset_id": document["dataset_id"],
        "base_version": document["base_version"],
        "target_version": document["target_version"],
        "base_fingerprint": document["base_fingerprint"],
        "target_fingerprint": document["target_fingerprint"],
        "records": len(document["records"]),
        "output": str(output_path),
    }
