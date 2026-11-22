"""Strict, bounded JSON input and atomic file helpers."""

import json
import os
from pathlib import Path
import tempfile

# Shared size caps keep malformed or hostile files from becoming memory problems.
MAX_JSON_BYTES = 8_000_000
MAX_STORE_BYTES = 256_000_000


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def constant(value):
    raise ValueError("nonfinite JSON value")


def decode(raw):
    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
    )


def read_bytes(path):
    with open(path, "rb") as f:
        raw = f.read(MAX_STORE_BYTES + 1)
    if len(raw) > MAX_STORE_BYTES:
        raise ValueError("file over 256MB")
    return raw


def load(path, max_bytes=MAX_JSON_BYTES):
    with open(path, "rb") as f:
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"input over {max_bytes // (1024 * 1024)}MB")
    return decode(raw)


def dump_store(value):
    """Stable whitespace for on-disk state and human-facing reports."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_temporary(target, payload):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".publish-", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def replace_atomic(path, payload):
    """Write payload to a sibling temporary file and rename over the target."""
    target = Path(path)
    temporary = _write_temporary(target, payload)
    try:
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_new(path, payload):
    """Create one new output file, never overwriting an existing name."""
    target = Path(path)
    temporary = _write_temporary(target, payload)
    try:
        os.link(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
