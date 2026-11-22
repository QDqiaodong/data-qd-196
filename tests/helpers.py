"""Shared helpers for the offline delivery tests."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

LEFT = {
    "id": "left",
    "projection": [["1", "0", "0", "0"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
}
RIGHT = {
    "id": "right",
    "projection": [["1", "0", "0", "-1"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
}


def observations():
    return [
        {"camera_id": "left", "xy": ["0.5", "0.25"], "weight": "1"},
        {"camera_id": "right", "xy": ["0.25", "0.25"], "weight": "1"},
    ]


def build_document(dataset_id="site-a", threshold="0", sequences=(10, 20, 30)):
    return copy.deepcopy({
        "dataset_id": dataset_id,
        "max_reprojection_error": threshold,
        "cameras": [LEFT, RIGHT],
        "batches": [
            {"sequence": sequence, "tracks": [{"id": "marker", "observations": observations()}]}
            for sequence in sequences
        ],
    })


def change_document(change_id, base_version, replacements, dataset_id="site-a"):
    return {
        "dataset_id": dataset_id,
        "change_id": change_id,
        "base_version": base_version,
        "replacements": replacements,
    }


def right_projection(last="-1"):
    return [["1", "0", "0", last], ["0", "1", "0", "0"], ["0", "0", "1", "0"]]


def replacement(start, end, last="-1", camera="right"):
    return {
        "camera_id": camera,
        "start": start,
        "end": end,
        "projection": right_projection(last),
    }


def write_json(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")
    return Path(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_cli(*args, fault=None, check=True):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    environment.pop("CAMERA_TRACKS_FAULT", None)
    if fault is not None:
        environment["CAMERA_TRACKS_FAULT"] = fault
    completed = subprocess.run(
        [sys.executable, "-m", "camera_tracks.cli", *map(str, args)],
        capture_output=True,
        text=True,
        env=environment,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {completed.stderr}"
        )
    return completed
