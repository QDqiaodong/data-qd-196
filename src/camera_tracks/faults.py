"""Deterministic, test-only interruption points.

Set ``CAMERA_TRACKS_FAULT`` to one of the names below to make the command
fail at a precise point.  The points are deliberately inside the write paths,
after all validation, so tests can exercise real atomicity boundaries rather
than merely rejecting input early.
"""

import os

BUILD_BEFORE_COMMIT = "build-before-commit"
CORRECT_BEFORE_COMMIT = "correct-before-commit"
CORRECT_AFTER_COMMIT = "correct-after-commit"
EXPORT_BEFORE_WRITE = "export-before-write"
APPLY_BEFORE_COMMIT = "apply-before-commit"
APPLY_AFTER_COMMIT = "apply-after-commit"
MERGE_BEFORE_WRITE = "merge-before-write"

_NAMES = {
    BUILD_BEFORE_COMMIT,
    CORRECT_BEFORE_COMMIT,
    CORRECT_AFTER_COMMIT,
    EXPORT_BEFORE_WRITE,
    APPLY_BEFORE_COMMIT,
    APPLY_AFTER_COMMIT,
    MERGE_BEFORE_WRITE,
}


def checkpoint(name):
    configured = os.environ.get("CAMERA_TRACKS_FAULT")
    if configured:
        if configured not in _NAMES:
            raise ValueError(f"unknown fault injection point: {configured}")
        if configured == name:
            raise RuntimeError(f"injected interruption at {name}")
