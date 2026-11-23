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


_MEASURE_SCRIPT = r"""
import json, os, sys
spec = json.loads(sys.argv[1])
pid = os.fork()
if pid == 0:
    with open(spec["stdout"], "wb") as out, open(spec["stderr"], "wb") as err:
        os.dup2(out.fileno(), 1)
        os.dup2(err.fileno(), 2)
        os.execvpe(spec["argv"][0], spec["argv"], spec["env"])
_, status, usage = os.wait4(pid, 0)
print(json.dumps({
    "code": os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1,
    "cpu": usage.ru_utime + usage.ru_stime,
    "rss": usage.ru_maxrss * 1024,
}))
"""


def measure_cli(*args):
    """Run the CLI as one isolated child; return (result, (cpu, peak_rss)).

    A small intermediate interpreter forks and execs the target, so the
    measured peak RSS covers the CLI run itself and not this test process's
    own resident pages inherited across ``fork``.  CPU time and peak RSS come
    from ``os.wait4`` for that single child (Linux).
    """
    import tempfile

    directory = tempfile.mkdtemp(prefix="measure-")
    stdout_path = os.path.join(directory, "stdout")
    stderr_path = os.path.join(directory, "stderr")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    environment.pop("CAMERA_TRACKS_FAULT", None)
    argv = [sys.executable, "-m", "camera_tracks.cli", *map(str, args)]
    spec = json.dumps(
        {
            "argv": argv,
            "env": environment,
            "stdout": stdout_path,
            "stderr": stderr_path,
        }
    )
    try:
        completed = subprocess.run(
            [
                sys.executable, "-I", "-c", _MEASURE_SCRIPT, spec,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        usage = json.loads(completed.stdout)
        out_text = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
        err_text = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
    finally:
        for name in ("stdout", "stderr"):
            Path(directory, name).unlink(missing_ok=True)
        Path(directory).rmdir()
    result = subprocess.CompletedProcess(
        argv, usage["code"], out_text, err_text
    )
    return result, (usage["cpu"], usage["rss"])
