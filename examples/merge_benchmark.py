#!/usr/bin/env python3
"""Reproducible 60,000-fragment merge benchmark (造数、计时与逐条核对).

Builds the acceptance batch described in docs/merge.md: V0..V59999 chain ties
e_i, the long tie f and the two reviewed ties g1/g2.  Two small point tables
are bound to V20000 (released) and V20001 (withheld); only V20000's point may
be delivered.  Input generation is not timed.  Usage::

    python3 examples/merge_benchmark.py [WORKDIR]

The benchmark command is run as one isolated child process, so the reported
peak RSS covers the merge run itself and not this generator's own pages.
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
SIZE = 60_000

sys.path.insert(0, str(SRC))
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402


def fragment_id(i):
    return f"V{i:05d}"


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
    links = [
        {
            "record_id": f"e{i}",
            "from_fragment": fragment_id(i - 1),
            "to_fragment": fragment_id(i),
            "delta": [1_000_000, 0, 0],
            "status": "tentative",
        }
        for i in range(1, SIZE)
    ]
    links += [
        {
            "record_id": "f",
            "from_fragment": fragment_id(0),
            "to_fragment": fragment_id(SIZE - 1),
            "delta": [60_000_000_000, 0, 0],
            "status": "tentative",
        },
        {
            "record_id": "g1",
            "from_fragment": fragment_id(0),
            "to_fragment": fragment_id(20_000),
            "delta": [20_000_000_000, 0, 0],
            "status": "reviewed",
        },
        {
            "record_id": "g2",
            "from_fragment": fragment_id(40_000),
            "to_fragment": fragment_id(SIZE - 1),
            "delta": [19_999_000_000, 0, 0],
            "status": "reviewed",
        },
    ]
    view_20k = view_document({(1, "p"): ["0", "0", "0"]}, "dsV20000")
    view_20k1 = view_document({(1, "p"): ["0", "0", "0"]}, "dsV20001")
    endpoint_map = {
        fragment_id(20_000): endpoint(view_20k),
        fragment_id(20_001): endpoint(view_20k1),
    }
    plan = {
        "schema_version": 1,
        "kind": "camera-tracks-merge-plan",
        "plan_id": "bench-merge",
        "baseline": fragment_id(0),
        "fragments": [
            {
                "fragment_id": fragment_id(i),
                "endpoint": endpoint_map.get(fragment_id(i)),
            }
            for i in range(SIZE)
        ],
        "links": links,
    }
    write_json(work / "plan.json", plan)
    write_json(work / "view-v20000.json", view_20k)
    write_json(work / "view-v20001.json", view_20k1)
    return plan


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "run" / "merge-benchmark")
    if work.exists():
        for entry in work.iterdir():
            entry.unlink()
    work.mkdir(parents=True, exist_ok=True)
    plan = generate(work)
    output = work / "merge-package.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    argv = [
        sys.executable, "-m", "camera_tracks.cli", "merge",
        "--plan", work / "plan.json",
        "--output", output,
        "--view", f"{fragment_id(20_000)}={work / 'view-v20000.json'}",
        "--view", f"{fragment_id(20_001)}={work / 'view-v20001.json'}",
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        argv, capture_output=True, text=True, env=environment, check=True
    )
    elapsed = time.perf_counter() - started
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    package = json.loads(output.read_text(encoding="utf-8"))
    expected = {"f"} | {f"e{i}" for i in range(20_001, 40_001)}
    assert set(package["feasible_discards"]) == expected
    assert package["released_count"] == 20_001
    assert package["withheld_count"] == 39_999
    assert package["not_released_count"] == 0
    assert package["point_count"] == 1
    point = package["points"][0]
    assert (point["fragment_id"], point["sequence"], point["track_id"]) == (
        fragment_id(20_000), 1, "p"
    )
    assert point["xyz"] == ["20000", "0", "0"]
    # Every fragment decision is present and every withheld fragment carries
    # two distinct, recomputable evidences.
    assert len(package["fragments"]) == SIZE
    withheld = [f for f in package["fragments"] if f["decision"] == "withheld"]
    assert len(withheld) == 39_999
    assert all(len(f["evidence"]) == 2 for f in withheld)
    assert all(
        f["evidence"][0]["origin_um"] != f["evidence"][1]["origin_um"]
        for f in withheld
    )
    print(
        f"fragments={SIZE} links={len(plan['links'])} "
        f"candidates={len(package['feasible_discards'])} "
        f"released={package['released_count']} withheld={package['withheld_count']} "
        f"points={package['point_count']} "
        f"wall={elapsed:.2f}s cpu={usage.ru_utime + usage.ru_stime:.2f}s "
        f"peak_rss={usage.ru_maxrss / 1024:.0f} MiB"
    )
    print(f"  stdout: {completed.stdout.strip()}")
    print(f"  package: {output}")


if __name__ == "__main__":
    main()
