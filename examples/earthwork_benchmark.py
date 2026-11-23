#!/usr/bin/env python3
"""Reproducible earthwork capacity and acceptance benchmarks (容量样例).

Two independent samples, generated in a fresh directory; input generation
and the independent verification are not charged to the command budget:

1. ``strips`` — a 317 x 317 work zone.  The old ground is 317 vertical
   1 m strips and the new ground 317 horizontal 1 m strips, so a complete
   settlement has 100,489 slices and must be refused.  Removing one anchor
   that no block uses still leaves 100,489 slices; removing one anchor from
   each of two strips leaves 99,855 slices and publishes, with 634 m^2
   unsettled.
2. ``batch`` — 140 x 140 blocks per layer with 2L x 2L blocks and
   L = 1,000,000 m.  The old layer tiles [0, 0, 280L, 280L] at elevation 0,
   the new layer tiles it shifted by (+L, +L) at elevation 2, and the zone
   is [0, 0, 281L, 281L].  Exactly 279^2 = 77,841 slices must be emitted;
   the last case is re-run with every plan declaration shuffled and the
   package bytes must be identical.  Both runs are measured for CPU time
   and peak RSS with ``os.wait4`` (Linux).

Usage::

    python3 examples/earthwork_benchmark.py [WORKDIR]
"""

from fractions import Fraction
import json
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
from camera_tracks.state import fingerprint_points, point_key  # noqa: E402

STRIPS = 317
BLOCKS = 140
L = 1_000_000


def write_json(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def view_document(points, dataset_id):
    table = {point_key(s, t): list(xyz) for (s, t), xyz in points.items()}
    return {
        "schema_version": 1,
        "kind": "camera-tracks-view",
        "dataset_id": dataset_id,
        "version": 0,
        "fingerprint": fingerprint_points(table),
        "points": [
            {"sequence": s, "track_id": t, "xyz": list(xyz)}
            for (s, t), xyz in sorted(points.items())
        ],
    }


def endpoint(view):
    return {
        "dataset_id": view["dataset_id"],
        "version": view["version"],
        "fingerprint": view["fingerprint"],
    }


def plan_document(zone_id, zone, old, new, old_blocks, new_blocks):
    return {
        "schema_version": 1,
        "kind": "camera-tracks-earthwork-plan",
        "zone_id": zone_id,
        "zone": list(zone),
        "old": {**endpoint(old), "blocks": old_blocks},
        "new": {**endpoint(new), "blocks": new_blocks},
    }


def ref(sequence, track_id):
    return {"sequence": sequence, "track_id": track_id}


def run_command(work, plan_name, report_name):
    """Run one earthwork command; return (returncode, cpu seconds, rss bytes)."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    argv = [
        sys.executable, "-m", "camera_tracks.cli", "earthwork",
        "--old", str(work / f"{plan_name}-old.json"),
        "--new", str(work / f"{plan_name}-new.json"),
        "--plan", str(work / f"{plan_name}.json"),
        "--output", str(work / report_name),
    ]
    pid = os.fork()
    if pid == 0:  # child
        os.execvpe(argv[0], argv, environment)
    _, status, usage = os.wait4(pid, 0)
    if not os.WIFEXITED(status):
        raise SystemExit(f"command died with status {status}: {argv}")
    cpu = usage.ru_utime + usage.ru_stime
    rss = usage.ru_maxrss * 1024  # Linux reports KiB
    return os.WEXITSTATUS(status), cpu, rss


# ---------------------------------------------------------------------------
# Strips sample


def strips_document(removed):
    """317 vertical old strips over 317 horizontal new strips.

    ``removed`` is a set of ``(column, y)`` identities the old view omits;
    a removed anchor at y=0 or y=N affects the strip whose corner uses it.
    """
    old_points = {}
    for i in range(STRIPS + 1):
        for y in (0, STRIPS):
            if (i, y) in removed:
                continue
            old_points[(i, f"g{i}_{y}")] = [str(i), str(y), "0"]
    old_blocks = [
        {
            "block_id": f"v{i}",
            "area": [i, 0, i + 1, STRIPS],
            "lower_left": ref(i, f"g{i}_0"),
            "lower_right": ref(i + 1, f"g{i + 1}_0"),
            "upper_left": ref(i, f"g{i}_{STRIPS}"),
        }
        for i in range(STRIPS)
    ]
    new_points = {}
    for j in range(STRIPS + 1):
        for x in (0, STRIPS):
            new_points[(j, f"h{j}_{x}")] = [str(x), str(j), "1"]
    new_blocks = [
        {
            "block_id": f"h{j}",
            "area": [0, j, STRIPS, j + 1],
            "lower_left": ref(j, f"h{j}_0"),
            "lower_right": ref(j, f"h{j}_{STRIPS}"),
            "upper_left": ref(j + 1, f"h{j+1}_0"),
        }
        for j in range(STRIPS)
    ]
    old = view_document(old_points, "strips-old")
    new = view_document(new_points, "strips-new")
    plan = plan_document(
        "strips", (0, 0, STRIPS, STRIPS), old, new, old_blocks, new_blocks
    )
    return old, new, plan


def generate_strips(work):
    cases = {
        # every anchor present: 317 * 317 = 100,489 settled slices
        "strips-full": set(),
        # one strip loses its corner anchor, used by no other block:
        # 316 * 317 = 100,172 settled slices, still over the limit
        "strips-one-gap": {(0, 0)},
        # two strips lose one corner each: 315 * 317 = 99,855 slices
        "strips-two-gaps": {(0, 0), (STRIPS, 0)},
    }
    for name, removed in cases.items():
        old, new, plan = strips_document(removed)
        write_json(work / f"{name}-old.json", old)
        write_json(work / f"{name}-new.json", new)
        write_json(work / f"{name}.json", plan)


# ---------------------------------------------------------------------------
# Batch sample


def batch_documents():
    def grid_points(offset, height):
        points = {}
        for i in range(BLOCKS + 1):
            for j in range(BLOCKS + 1):
                points[(i * 1000 + j, f"p{i}_{j}")] = [
                    str(offset + i * 2 * L),
                    str(offset + j * 2 * L),
                    height,
                ]
        return points

    def blocks(offset, tag):
        result = []
        for i in range(BLOCKS):
            for j in range(BLOCKS):
                result.append(
                    {
                        "block_id": f"{tag}{i}_{j}",
                        "area": [
                            offset + i * 2 * L,
                            offset + j * 2 * L,
                            offset + (i + 1) * 2 * L,
                            offset + (j + 1) * 2 * L,
                        ],
                        "lower_left": ref(i * 1000 + j, f"p{i}_{j}"),
                        "lower_right": ref((i + 1) * 1000 + j, f"p{i + 1}_{j}"),
                        "upper_left": ref(i * 1000 + j + 1, f"p{i}_{j + 1}"),
                    }
                )
        return result

    old = view_document(grid_points(0, "0"), "batch-old")
    new = view_document(grid_points(L, "2"), "batch-new")
    plan = plan_document(
        "batch",
        (0, 0, 281 * L, 281 * L),
        old,
        new,
        blocks(0, "o"),
        blocks(L, "n"),
    )
    return old, new, plan


def generate_batch(work, shuffle_seed=None):
    old, new, plan = batch_documents()
    if shuffle_seed is not None:
        rng = random.Random(shuffle_seed)
        for layer in ("old", "new"):
            rng.shuffle(plan[layer]["blocks"])
        rng.shuffle(old["points"])
        rng.shuffle(new["points"])
    write_json(work / "batch-old.json", old)
    write_json(work / "batch-new.json", new)
    write_json(work / "batch.json", plan)


# ---------------------------------------------------------------------------
# Independent verification


def slices_of(package):
    """Every slice must sit inside the zone and name its two source blocks."""
    zone = package["zone"]
    zone_area = (zone[2] - zone[0]) * (zone[3] - zone[1])
    total = 0
    for entry in package["slices"]:
        left, bottom, right, top = entry["range"]
        assert zone[0] <= left < right <= zone[2], entry
        assert zone[1] <= bottom < top <= zone[3], entry
        assert entry["old_block"] and entry["new_block"]
        assert set(entry["corner_differences"]) == {
            "lower_left", "lower_right", "upper_right", "upper_left",
        }
        total += (right - left) * (top - bottom)
    assert int(package["settled_area"]) == total, (total, package["settled_area"])
    assert total + int(package["unsettled_area"]) == zone_area
    return total


def verify_strips(work, name, published, expected_slices=None,
                  expected_unsettled=None):
    target = work / f"{name}-package.json"
    if not published:
        assert not target.exists(), f"{name} published a package despite refusal"
        return
    package = json.loads(target.read_text(encoding="utf-8"))
    total = slices_of(package)
    assert len(package["slices"]) == expected_slices, (
        len(package["slices"]), expected_slices
    )
    assert total == expected_slices
    assert int(package["unsettled_area"]) == expected_unsettled
    assert package["status"] == "partial"
    assert package["settled_cut"] == "0"
    # New strips are 1 m higher than old ones, so fill is the whole volume.
    assert Fraction(package["settled_fill"]) == Fraction(expected_slices)
    print(
        f"  verified {name}: {len(package['slices'])} slices, "
        f"unsettled {package['unsettled_area']} m^2"
    )


def verify_batch(work, name):
    package = json.loads((work / f"{name}-package.json").read_text(encoding="utf-8"))
    total = slices_of(package)
    # Each 2L block shifted by L overlaps two neighbours per axis (one at the
    # border), so 2*140 - 1 = 279 old/new block pairs overlap per axis.
    axis = 2 * BLOCKS - 1
    expected = axis * axis
    assert len(package["slices"]) == expected, (len(package["slices"]), expected)
    assert total == expected * L * L
    assert int(package["settled_area"]) == expected * L * L
    assert int(package["unsettled_area"]) == (281**2 - expected) * L * L
    assert int(package["zone_area"]) == 281**2 * L * L
    assert Fraction(package["settled_fill"]) == 2 * expected * L * L
    assert Fraction(package["settled_cut"]) == 0
    assert package["status"] == "partial"
    # Every slice must be the old/new block intersection; check by re-deriving
    # each block's rectangle from its declaration and intersecting them.
    plan = json.loads((work / "batch.json").read_text(encoding="utf-8"))
    old_area = {b["block_id"]: b["area"] for b in plan["old"]["blocks"]}
    new_area = {b["block_id"]: b["area"] for b in plan["new"]["blocks"]}
    seen = set()
    expected_pairs = set()
    for i in range(BLOCKS):
        for j in range(BLOCKS):
            for k in (i - 1, i):
                for l in (j - 1, j):
                    if not (0 <= k < BLOCKS and 0 <= l < BLOCKS):
                        continue
                    a = old_area[f"o{i}_{j}"]
                    b = new_area[f"n{k}_{l}"]
                    left, bottom = max(a[0], b[0]), max(a[1], b[1])
                    right, top = min(a[2], b[2]), min(a[3], b[3])
                    if left < right and bottom < top:
                        expected_pairs.add(
                            (f"o{i}_{j}", f"n{k}_{l}", left, bottom, right, top)
                        )
    for entry in package["slices"]:
        key = (
            entry["old_block"],
            entry["new_block"],
            *entry["range"],
        )
        assert key in expected_pairs, key
        seen.add(key)
    assert seen == expected_pairs
    return (work / f"{name}-package.json").read_bytes()


# ---------------------------------------------------------------------------
# Main


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "run" / "earthwork-benchmark")
    if work.exists():
        for entry in work.iterdir():
            entry.unlink()
    work.mkdir(parents=True, exist_ok=True)
    generate_strips(work)
    generate_batch(work)

    print("strips capacity (budget 20 s CPU / 192 MiB per run)", flush=True)
    outcomes = [
        ("strips-full", False),
        ("strips-one-gap", False),
        ("strips-two-gaps", True),
    ]
    for name, published in outcomes:
        status, cpu, rss = run_command(
            work, name, f"{name}-package.json"
        )
        assert (status == 0) == published, (name, status)
        print(
            f"  {name}: exit={status} cpu={cpu:.2f}s "
            f"peak_rss={rss / 2**20:.0f} MiB",
            flush=True,
        )
        if name == "strips-two-gaps":
            verify_strips(
                work, name, published, expected_slices=315 * 317,
                expected_unsettled=STRIPS * STRIPS - 315 * STRIPS,
            )
        else:
            verify_strips(work, name, published)

    print("batch acceptance (budget 20 s CPU / 192 MiB per run)", flush=True)
    status, cpu, rss = run_command(work, "batch", "batch-package.json")
    assert status == 0, status
    original = verify_batch(work, "batch")
    print(
        f"  batch: 77,841 slices, cpu={cpu:.2f}s "
        f"peak_rss={rss / 2**20:.0f} MiB",
        flush=True,
    )
    generate_batch(work, shuffle_seed=196)
    status, cpu, rss = run_command(work, "batch", "batch-shuffled-package.json")
    assert status == 0, status
    shuffled = verify_batch(work, "batch-shuffled")
    assert original == shuffled, "shuffling declarations changed the package bytes"
    print(
        f"  batch (shuffled declarations): identical bytes, cpu={cpu:.2f}s "
        f"peak_rss={rss / 2**20:.0f} MiB",
        flush=True,
    )
    print(f"artifacts in {work}")


if __name__ == "__main__":
    main()
