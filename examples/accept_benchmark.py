#!/usr/bin/env python3
"""Reproducible 4096-point acceptance benchmark (样例输入生成与计时).

Generates the two documented benchmark cases in a fresh directory and runs
``camera-tracks accept`` on each, reporting wall/CPU time and peak RSS of
every command.  Input generation is not timed.  Usage::

    python3 examples/accept_benchmark.py [WORKDIR]

Both cases put 4096 points per side at (i, 0, 0); every baseline point i has
candidates resurvey i and (i+1) mod 4096, and the displacement limit is 0.

* open:      no confirmations -> every point has two possible partners,
             min 0 / max 4096 exceeded, conclusion ``verify_labels``.
* confirmed: one cross pairing confirmed -> every point is forced onto its
             cross partner, min = max = 4096 exceeded, conclusion ``fail``.
"""

import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SIZE = 4096

sys.path.insert(0, str(SRC))
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402


def view_document(points, dataset_id):
    table = {point_key(s, t): xyz for (s, t), xyz in points.items()}
    return {
        "schema_version": 1,
        "kind": "camera-tracks-view",
        "dataset_id": dataset_id,
        "version": 0,
        "fingerprint": fingerprint_points(table),
        "points": [
            {"sequence": s, "track_id": t, "xyz": xyz}
            for (s, t), xyz in sorted(points.items())
        ],
    }


def endpoint(view):
    return {
        "dataset_id": view["dataset_id"],
        "version": view["version"],
        "fingerprint": view["fingerprint"],
    }


def write_json(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def generate(work):
    n = SIZE
    baseline = view_document({(i, "p"): [str(i), "0", "0"] for i in range(n)}, "bench-base")
    resurvey = view_document({(i, "q"): [str(i), "0", "0"] for i in range(n)}, "bench-re")
    write_json(work / "baseline-view.json", baseline)
    write_json(work / "resurvey-view.json", resurvey)
    candidates = [
        {"baseline": {"sequence": i, "track_id": "p"},
         "resurvey": {"sequence": i, "track_id": "q"}}
        for i in range(n)
    ] + [
        {"baseline": {"sequence": i, "track_id": "p"},
         "resurvey": {"sequence": (i + 1) % n, "track_id": "q"}}
        for i in range(n)
    ]
    zone = {
        "schema_version": 1,
        "kind": "camera-tracks-acceptance",
        "zone_id": "bench-open",
        "displacement_limit": "0",
        "baseline": endpoint(baseline),
        "resurvey": endpoint(resurvey),
        "baseline_required": [{"sequence": i, "track_id": "p"} for i in range(n)],
        "resurvey_required": [{"sequence": i, "track_id": "q"} for i in range(n)],
        "candidates": candidates,
        "confirmations": [],
    }
    write_json(work / "zone-open.json", zone)
    confirmed = dict(zone)
    confirmed["zone_id"] = "bench-confirmed"
    confirmed["confirmations"] = [
        {"baseline": {"sequence": 0, "track_id": "p"},
         "resurvey": {"sequence": 1, "track_id": "q"}}
    ]
    write_json(work / "zone-confirmed.json", confirmed)


def run_case(work, config_name, report_name):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    completed = subprocess.run(
        [
            sys.executable, "-m", "camera_tracks.cli", "accept",
            "--baseline", work / "baseline-view.json",
            "--resurvey", work / "resurvey-view.json",
            "--config", work / config_name,
            "--output", work / report_name,
        ],
        capture_output=True, text=True, env=environment, check=True,
    )
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    # ru_maxrss is the peak over all waited-for children so far; run each
    # case in a fresh helper process when exact per-case peaks are needed.
    report = json.loads((work / report_name).read_text(encoding="utf-8"))
    print(
        f"{config_name}: conclusion={report['conclusion']} "
        f"min={report['exceeded']['min']['count']} "
        f"max={report['exceeded']['max']['count']} "
        f"wall={elapsed:.2f}s cpu={cpu:.2f}s peak_rss={after.ru_maxrss / 1024:.0f} MiB"
    )
    print(f"  stdout: {completed.stdout.strip()}")


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "run" / "accept-benchmark")
    if work.exists():
        for entry in work.iterdir():
            entry.unlink()
    work.mkdir(parents=True, exist_ok=True)
    generate(work)
    run_case(work, "zone-open.json", "report-open.json")
    run_case(work, "zone-confirmed.json", "report-confirmed.json")
    print(f"artifacts in {work}")


if __name__ == "__main__":
    main()
