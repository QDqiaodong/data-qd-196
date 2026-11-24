#!/usr/bin/env python3
"""Reproducible benchmark-project acceptance batch (4 views, 80,000 points).

Generates four valid ``view`` exports of 100 batches x 200 points each and a
configuration whose reviewed records chain adjacent batch representatives
inside every view, so each view forms exactly one relation group.  Then, with
each command run as one isolated child process, it:

1. initialises the project (80,000 frozen points);
2. issues a 4096-point booklet for the second and third views and registers
   one prediction for each;
3. releases a single point of the first view (no download);
4. appends one relation between representatives of the first and second
   views;
5. verifies that the second booklet is withdrawn and the third stays usable,
   that both complete booklets re-export byte for byte, and that the old
   receipts replay unchanged.

Input generation is not timed or charged.  Usage::

    python3 examples/bench_benchmark.py [WORKDIR]

Every measured run is charged to the same CPU-time budget (45 s) and peak
RSS budget (256 MiB) from ``os.wait4`` on the single command child.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VIEWS = 4
BATCHES = 100
PER_BATCH = 200
BOOKLET_POINTS = 4096

sys.path.insert(0, str(SRC))
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402

CPU_BUDGET = 45.0
RSS_BUDGET = 256 * 1024 * 1024

_MEASURE = r"""
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


def view_document(dataset_id, points):
    table = {point_key(sequence, track): xyz for (sequence, track), xyz in points.items()}
    return {
        "schema_version": 1,
        "kind": "camera-tracks-view",
        "dataset_id": dataset_id,
        "version": 0,
        "fingerprint": fingerprint_points(table),
        "points": [
            {"sequence": sequence, "track_id": track, "xyz": xyz}
            for (sequence, track), xyz in sorted(points.items())
        ],
    }


def ref(dataset_id, sequence, track_id):
    return {"dataset_id": dataset_id, "sequence": sequence, "track_id": track_id}


def write_json(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def true_z(work, dataset_id, sequence, track_id):
    document = json.loads((work / f"view-{dataset_id}.json").read_text(encoding="utf-8"))
    for row in document["points"]:
        if row["sequence"] == sequence and row["track_id"] == track_id:
            return row["xyz"][2]
    raise KeyError((dataset_id, sequence, track_id))


def generate(work):
    datasets = [f"ds{i}" for i in range(VIEWS)]
    relations = []
    for view_index, dataset_id in enumerate(datasets):
        points = {}
        for batch in range(BATCHES):
            for index in range(PER_BATCH):
                # Distinct, exactly representable Z per point.
                points[(batch, f"p{index:03d}")] = [
                    str(batch), str(index), str(1000 + view_index * 100 + batch)
                ]
        write_json(work / f"view-{dataset_id}.json", view_document(dataset_id, points))
        for batch in range(BATCHES - 1):
            relations.append({"points": [
                ref(dataset_id, batch, "p000"),
                ref(dataset_id, batch + 1, "p000"),
            ]})

    config = {
        "schema_version": 1,
        "kind": "camera-tracks-benchmark-config",
        "project_id": "bench-capacity",
        "datasets": [
            {
                "dataset_id": dataset_id,
                "version": 0,
                "fingerprint": json.loads(
                    (work / f"view-{dataset_id}.json").read_text(encoding="utf-8")
                )["fingerprint"],
            }
            for dataset_id in datasets
        ],
        "relations": relations,
    }
    write_json(work / "config.json", config)

    for view_index in (1, 2):
        dataset_id = datasets[view_index]
        points = [
            ref(dataset_id, batch, f"p{index:03d}")
            for batch in range(BATCHES)
            for index in range(PER_BATCH)
        ][:BOOKLET_POINTS]
        assert len(points) == BOOKLET_POINTS
        write_json(work / f"issue-{dataset_id}.json", {
            "op_id": f"issue-{dataset_id}",
            "booklet_id": f"book-{dataset_id}",
            "elevation_limit": "1",
            "points": points,
        })

    write_json(work / "release-ds0.json", {
        "op_id": "release-ds0",
        "release_id": "train-ds0",
        "points": [ref("ds0", 0, "p000")],
    })
    write_json(work / "link01.json", {
        "op_id": "link01",
        "relations": [{"points": [ref("ds1", 0, "p000"), ref("ds0", 0, "p000")]}],
    })
    return datasets


class Measured:
    def __init__(self):
        self.cpu = 0.0
        self.peak = 0
        self.commands = []

    def run(self, work, *args, env_extra=None):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC)
        environment.pop("CAMERA_TRACKS_FAULT", None)
        if env_extra:
            environment.update(env_extra)
        stdout_path = work / ".stdout"
        stderr_path = work / ".stderr"
        spec = json.dumps({
            "argv": [sys.executable, "-m", "camera_tracks.cli", *map(str, args)],
            "env": environment,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        })
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _MEASURE, spec],
            capture_output=True, text=True, check=True,
        )
        wall = time.perf_counter() - started
        usage = json.loads(completed.stdout)
        out = stdout_path.read_text(encoding="utf-8")
        err = stderr_path.read_text(encoding="utf-8")
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        if usage["code"] != 0:
            raise SystemExit(f"command failed: {args}\n{err}")
        self.cpu += usage["cpu"]
        self.peak = max(self.peak, usage["rss"])
        self.commands.append((args[0], usage["cpu"], usage["rss"], wall))
        return json.loads(out)


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "run" / "bench-benchmark")
    work.mkdir(parents=True, exist_ok=True)
    for entry in work.iterdir():
        if entry.is_file():
            entry.unlink()
    datasets = generate(work)

    meter = Measured()
    view_args = []
    for dataset_id in datasets:
        view_args += ["--view", f"{dataset_id}={work / f'view-{dataset_id}.json'}"]

    init = meter.run(work, "bench", "--config", work / "config.json",
                     "--project", work / "project.json", *view_args)
    assert init["points"] == VIEWS * BATCHES * PER_BATCH == 80_000, init

    receipts = {}
    for dataset_id in (datasets[1], datasets[2]):
        receipts[f"issue-{dataset_id}"] = meter.run(
            work, "bench-issue", "--project", work / "project.json",
            "--request", work / f"issue-{dataset_id}.json",
        )
        assert receipts[f"issue-{dataset_id}"]["points"] == BOOKLET_POINTS

    project = json.loads((work / "project.json").read_text(encoding="utf-8"))
    for dataset_id in (datasets[1], datasets[2]):
        booklet_id = f"book-{dataset_id}"
        booklet = project["booklets"][booklet_id]
        predictions = [
            {
                "dataset_id": dataset_id,
                "sequence": sequence,
                "track_id": track_id,
                # Half exact, half off by more than the limit.
                "z": true_z(work, dataset_id, sequence, track_id)
                if index % 2 == 0 else "0",
            }
            for index, (dataset_id, sequence, track_id) in enumerate(booklet["points"])
        ]
        write_json(work / f"score-{dataset_id}.json", {
            "op_id": f"score-{dataset_id}",
            "booklet_id": booklet_id,
            "booklet_sha256": booklet["sha256"],
            "prediction_id": f"pred-{dataset_id}-1",
            "predictions": predictions,
        })
        receipts[f"score-{dataset_id}"] = meter.run(
            work, "bench-score", "--project", work / "project.json",
            "--request", work / f"score-{dataset_id}.json",
        )
        assert receipts[f"score-{dataset_id}"]["total"] == BOOKLET_POINTS

    # Export both booklets before any withdrawal; these bytes must survive.
    exported = {}
    for dataset_id in (datasets[1], datasets[2]):
        output = work / f"book-{dataset_id}.json"
        summary = meter.run(
            work, "bench-export-booklet", "--project", work / "project.json",
            "--id", f"book-{dataset_id}", "--output", output,
        )
        exported[dataset_id] = (output, summary["sha256"])

    released = meter.run(work, "bench-release", "--project", work / "project.json",
                         "--request", work / "release-ds0.json")
    assert released["withdrawn"] == [], released
    tied = meter.run(work, "bench-relate", "--project", work / "project.json",
                     "--request", work / "link01.json")
    assert tied["withdrawn"] == ["book-ds1"], tied

    scores = meter.run(work, "bench-scores", "--project", work / "project.json")
    comparable = {row["booklet_id"] for row in scores["scores"]}
    assert comparable == {"book-ds2"}, scores
    audit = meter.run(work, "bench-audit", "--project", work / "project.json")
    by_id = {row["booklet_id"]: row for row in audit["booklets"]}
    assert by_id["book-ds1"]["withdrawn"] is True
    assert by_id["book-ds2"]["withdrawn"] is False
    evidence = by_id["book-ds1"]["withdrawal"]
    assert evidence["release_id"] == "train-ds0"
    assert len(by_id["book-ds1"]["predictions"]) == 1

    # Complete booklets: re-export after withdrawal must be byte identical,
    # and no true Z may leak.
    for dataset_id in (datasets[1], datasets[2]):
        original, digest = exported[dataset_id]
        again = work / f"book-{dataset_id}-again.json"
        meter.run(
            work, "bench-export-booklet", "--project", work / "project.json",
            "--id", f"book-{dataset_id}", "--output", again,
        )
        assert original.read_bytes() == again.read_bytes()
        assert hashlib.sha256(again.read_bytes()).hexdigest() == digest
        document = json.loads(again.read_text(encoding="utf-8"))
        assert len(document["points"]) == BOOKLET_POINTS
        assert all(set(point) == {"dataset_id", "sequence", "track_id", "xy", "source"}
                   for point in document["points"])
        assert "z" not in again.read_text(encoding="utf-8")

    # History receipts replay unchanged (no new revision).
    for dataset_id in (datasets[1], datasets[2]):
        replay = meter.run(
            work, "bench-score", "--project", work / "project.json",
            "--request", work / f"score-{dataset_id}.json",
        )
        assert replay == receipts[f"score-{dataset_id}"], (dataset_id, replay)
    relink = meter.run(work, "bench-relate", "--project", work / "project.json",
                       "--request", work / "link01.json")
    assert relink == tied

    final = json.loads((work / "project.json").read_text(encoding="utf-8"))
    assert final["revision"] == len(final["operations"])

    assert meter.cpu <= CPU_BUDGET, meter.cpu
    assert meter.peak <= RSS_BUDGET, meter.peak

    print(f"views={VIEWS} batches/view={BATCHES} points/view={BATCHES * PER_BATCH} "
          f"booklets={BOOKLET_POINTS}x2 released=1 withdrawn=book-ds1")
    for name, cpu, rss, wall in meter.commands:
        print(f"  {name:22s} cpu={cpu:5.2f}s rss={rss / (1024 * 1024):6.1f}MiB "
              f"wall={wall:5.2f}s")
    print(f"total cpu={meter.cpu:.2f}s (budget {CPU_BUDGET:.0f}s) "
          f"peak_rss={meter.peak / (1024 * 1024):.1f}MiB "
          f"(budget {RSS_BUDGET / (1024 * 1024):.0f}MiB)")
    print(f"  project: {work / 'project.json'}")


if __name__ == "__main__":
    main()
