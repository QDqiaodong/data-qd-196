#!/usr/bin/env python3
"""Reproducible 10^9 x 10^9 resurvey capacity benchmark (容量样例).

Generates the two documented capacity cases in a fresh directory and runs
``camera-tracks resurvey`` on each, measuring CPU time and peak RSS of
every command run with ``os.wait4`` (Linux).  Input generation and the
independent report verification are not charged to the command budget
(20 s CPU time and 192 MiB peak RSS per run).  Usage::

    python3 examples/resurvey_benchmark.py [WORKDIR]

Both cases use a 10^9 x 10^9 unit grid at the origin, an empty view export,
and a forbidden middle column K = 500,000,000 plus 20,000 forbidden single
cells (2i+2, 2i+2) for i in 0..19999:

* open:   the middle column cell (K, K) is an exempt passage, so left and
          right stay connected -> one order with 10^18 - 10^9 - 20,000
          pending cells;
* closed: the gap cell is forbidden as well -> two orders with
          K*10^9 - 20,000 and (10^9 - K - 1)*10^9 pending cells.

Every report is verified independently: rectangles stay inside the grid,
are pairwise disjoint, cover no forbidden or passage cell, and their areas
reproduce the reported exact pending counts.
"""

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
K = 500_000_000
N = 10**9

sys.path.insert(0, str(SRC))
from camera_tracks.state import fingerprint_points  # noqa: E402


def write_json(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def generate(work):
    view = {
        "schema_version": 1,
        "kind": "camera-tracks-view",
        "dataset_id": "survey-cap",
        "version": 0,
        "fingerprint": fingerprint_points({}),
        "points": [],
    }
    write_json(work / "view.json", view)
    forbidden = [[K, 0, K + 1, K], [K, K + 1, K + 1, N]]
    forbidden += [[2 * i + 2, 2 * i + 2, 2 * i + 3, 2 * i + 3] for i in range(20_000)]
    plan = {
        "schema_version": 1,
        "kind": "camera-tracks-resurvey-plan",
        "zone_id": "zone-cap",
        "view": {
            "dataset_id": view["dataset_id"],
            "version": view["version"],
            "fingerprint": view["fingerprint"],
        },
        "grid": {"x0": "0", "y0": "0", "cell_size": "1", "columns": N, "rows": N},
        "z_min": "0",
        "z_max": "1",
        "required_sequences": 1,
        "pass_rectangles": [[K, K, K + 1, K + 1]],
        "forbidden_rectangles": forbidden,
    }
    write_json(work / "plan-open.json", plan)
    closed = dict(plan)
    closed["forbidden_rectangles"] = forbidden + [[K, K, K + 1, K + 1]]
    write_json(work / "plan-closed.json", closed)


def run_command(work, plan_name, report_name):
    """Run one resurvey command; return (cpu seconds, peak rss bytes)."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    argv = [
        sys.executable, "-m", "camera_tracks.cli", "resurvey",
        "--view", str(work / "view.json"),
        "--plan", str(work / plan_name),
        "--output", str(work / report_name),
    ]
    pid = os.fork()
    if pid == 0:  # child
        os.execvpe(argv[0], argv, environment)
    _, status, usage = os.wait4(pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise SystemExit(f"command failed with status {status}: {argv}")
    cpu = usage.ru_utime + usage.ru_stime
    rss = usage.ru_maxrss * 1024  # Linux reports KiB
    return cpu, rss


def union_area(rects):
    """Independent sweep-line union area of half-open integer rectangles."""
    events = []
    for left, bottom, right, top in rects:
        events.append((left, 1, bottom, top))
        events.append((right, -1, bottom, top))
    events.sort()
    area = 0
    active = []
    previous_x = 0
    for x, delta, bottom, top in events:
        if active and x > previous_x:
            active.sort()
            covered = 0
            span_bottom, span_top = active[0]
            for b, t in active[1:]:
                if b > span_top:
                    covered += span_top - span_bottom
                    span_bottom, span_top = b, t
                elif t > span_top:
                    span_top = t
            covered += span_top - span_bottom
            area += (x - previous_x) * covered
        previous_x = x
        if delta == 1:
            active.append((bottom, top))
        else:
            active.remove((bottom, top))
    return area


def verify(work, plan_name, report_name, expected_cells, expected_orders):
    plan = json.loads((work / plan_name).read_text(encoding="utf-8"))
    report = json.loads((work / report_name).read_text(encoding="utf-8"))
    assert report["cells"] == expected_cells, report["cells"]
    cells = report["cells"]
    total = sum(int(cells[name]) for name in ("accepted", "pending", "passage", "forbidden"))
    assert total == int(cells["total"]) == N * N
    assert [order["identity"] for order in report["orders"]] == [
        identity for identity, _count in expected_orders
    ]
    assert [order["pending_cells"] for order in report["orders"]] == [
        count for _identity, count in expected_orders
    ]
    obstacles = plan["forbidden_rectangles"] + plan["pass_rectangles"]
    obstacle_area = union_area(obstacles)
    assert obstacle_area == int(cells["forbidden"]) + int(cells["passage"])
    for order in report["orders"]:
        rects = order["rectangles"]
        for left, bottom, right, top in rects:
            assert 0 <= left < right <= N, rects
            assert 0 <= bottom < top <= N, rects
        plain = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
        assert plain == int(order["pending_cells"])
        order_area = union_area(rects)
        assert order_area == plain, "rectangles overlap"
        both = union_area(rects + obstacles)
        assert both == order_area + obstacle_area, "rectangles cover obstacles"
    print(f"  verified {report_name}: {len(report['orders'])} order(s), "
          f"{sum(len(o['rectangles']) for o in report['orders'])} rectangles")


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "run" / "resurvey-benchmark")
    if work.exists():
        for entry in work.iterdir():
            entry.unlink()
    work.mkdir(parents=True, exist_ok=True)
    generate(work)

    pending = str(N * N - N - 20_000)
    cases = [
        (
            "plan-open.json",
            "report-open.json",
            {"accepted": "0", "pending": pending, "passage": "1",
             "forbidden": str(N + 20_000 - 1), "total": str(N * N)},
            [([0, 0], pending)],
        ),
        (
            "plan-closed.json",
            "report-closed.json",
            {"accepted": "0", "pending": pending, "passage": "0",
             "forbidden": str(N + 20_000), "total": str(N * N)},
            [([0, 0], str(K * N - 20_000)), ([K + 1, 0], str((N - K - 1) * N))],
        ),
    ]
    for plan_name, report_name, expected_cells, expected_orders in cases:
        print(f"running {plan_name} ...", flush=True)
        cpu, rss = run_command(work, plan_name, report_name)
        print(f"{plan_name}: cpu={cpu:.2f}s peak_rss={rss / 2**20:.0f} MiB "
              f"(budget 20 s / 192 MiB)", flush=True)
        verify(work, plan_name, report_name, expected_cells, expected_orders)
    print(f"artifacts in {work}")


if __name__ == "__main__":
    main()
