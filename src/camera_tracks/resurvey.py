"""Offline resurvey work orders (补测工单) over a delivered point table.

The ``resurvey`` subcommand reads one complete ``view`` export and one zone
plan, then decides which grid cells still need another survey round and
groups them into machine-readable work orders for the field tools.

Every grid cell falls into exactly one class, decided in this order:

1. ``forbidden`` — the cell hits any no-entry rectangle, even if it is also
   declared exempt or already holds enough points;
2. ``passage`` — otherwise the cell hits an exempt passage rectangle; it
   needs no coverage and never changes with the point table;
3. ``accepted`` — otherwise the cell holds points from at least
   ``required_sequences`` distinct acquisition sequences (deduplicated by
   ``sequence``, not by track), using only points that land in the cell and
   whose elevation lies in ``[z_min, z_max)``;
4. ``pending`` — everything else, including cells with no points at all.

Points outside the grid give no coverage evidence.  Cell and elevation
assignment uses exact rational arithmetic on the original decimal strings;
no floating point is involved.

Pending and passage cells take part in dispatch connectivity along cell
edges (corner contact does not count); forbidden and accepted cells do not.
Every connected component that holds at least one pending cell becomes one
work order.  An order's measurement and construction scope contains only
its own pending cells — never the component bounding box, never passage
cells — and is encoded as a deterministic set of non-empty, pairwise
disjoint half-open integer rectangles whose union is exactly that pending
set.

The grid may be as large as 10^9 x 10^9 cells, so nothing per-cell is
materialised: the plan rectangles (at most 25,000) and the covered cells
(at most one per delivered point) are obstacles, and the free space is
decomposed into vertical elementary slabs whose strip count stays
proportional to the obstacle count, not to the grid area.

The command only reads its inputs: it never writes to the point table, the
plan, or any state store.  The report is created as one new file; an
existing report is never overwritten, and invalid input produces no report
at all.  Permuting the point or rectangle arrays, or reordering object
keys, does not change the report bytes.
"""

import bisect
from fractions import Fraction
from pathlib import Path

from . import io
from .checks import array, exact_array, fields, ident, integer, number, rational
from .state import (
    MAX_POINTS,
    MAX_SEQUENCE,
    MAX_VERSIONS,
    SCHEMA_VERSION,
    _digest,
    fingerprint_points,
    point_key,
)

VIEW_KIND = "camera-tracks-view"
PLAN_KIND = "camera-tracks-resurvey-plan"
REPORT_KIND = "camera-tracks-resurvey-report"

MAX_GRID_AXIS = 10**9
MAX_RECTANGLES = 25_000
MAX_REQUIRED_SEQUENCES = 100

# Cell classes; the numeric order is only for internal bookkeeping.
FORBIDDEN = 0
PASSAGE = 1
ACCEPTED = 2
PENDING = 3
_CONNECTABLE = (PASSAGE, PENDING)


# ---------------------------------------------------------------------------
# Zone plan


def _endpoint(value, where):
    fields(value, ("dataset_id", "version", "fingerprint"), where=where)
    ident(value["dataset_id"], f"{where} dataset_id")
    integer(value["version"], 0, MAX_VERSIONS, f"{where} version")
    _digest(value["fingerprint"], f"{where} fingerprint")
    return {
        "dataset_id": value["dataset_id"],
        "version": value["version"],
        "fingerprint": value["fingerprint"],
    }


def _rectangle(value, columns, rows, where):
    cells = exact_array(value, 4, where)
    left = integer(cells[0], 0, MAX_GRID_AXIS, f"{where} left")
    bottom = integer(cells[1], 0, MAX_GRID_AXIS, f"{where} bottom")
    right = integer(cells[2], 0, MAX_GRID_AXIS, f"{where} right")
    top = integer(cells[3], 0, MAX_GRID_AXIS, f"{where} top")
    if not (left < right <= columns and bottom < top <= rows):
        raise ValueError(
            f"{where} must be a non-empty rectangle inside the grid: "
            "0 <= left < right <= columns and 0 <= bottom < top <= rows"
        )
    return (left, bottom, right, top)


def load_plan(path):
    document = io.load(path)
    fields(
        document,
        (
            "schema_version",
            "kind",
            "zone_id",
            "view",
            "grid",
            "z_min",
            "z_max",
            "required_sequences",
            "pass_rectangles",
            "forbidden_rectangles",
        ),
        where="resurvey plan",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported resurvey plan schema_version")
    if document["kind"] != PLAN_KIND:
        raise ValueError("not a camera-tracks resurvey plan")
    zone_id = ident(document["zone_id"], "zone_id")
    endpoint = _endpoint(document["view"], "plan view")

    grid = fields(
        document["grid"],
        ("x0", "y0", "cell_size", "columns", "rows"),
        where="plan grid",
    )
    origin_x = number(grid["x0"], "grid x0")
    origin_y = number(grid["y0"], "grid y0")
    cell_size = number(grid["cell_size"], "grid cell_size")
    if cell_size <= 0:
        raise ValueError("grid cell_size must be positive")
    columns = integer(grid["columns"], 1, MAX_GRID_AXIS, "grid columns")
    rows = integer(grid["rows"], 1, MAX_GRID_AXIS, "grid rows")

    z_min = number(document["z_min"], "z_min")
    z_max = number(document["z_max"], "z_max")
    if not z_min < z_max:
        raise ValueError("z_min must be smaller than z_max")
    required = integer(
        document["required_sequences"], 1, MAX_REQUIRED_SEQUENCES,
        "required_sequences",
    )

    entries = array(
        document["pass_rectangles"], 0, MAX_RECTANGLES, "pass_rectangles"
    )
    pass_rectangles = [
        _rectangle(entry, columns, rows, f"pass_rectangles {index}")
        for index, entry in enumerate(entries)
    ]
    entries = array(
        document["forbidden_rectangles"], 0, MAX_RECTANGLES, "forbidden_rectangles"
    )
    forbidden_rectangles = [
        _rectangle(entry, columns, rows, f"forbidden_rectangles {index}")
        for index, entry in enumerate(entries)
    ]
    if len(pass_rectangles) + len(forbidden_rectangles) > MAX_RECTANGLES:
        raise ValueError(
            "pass_rectangles and forbidden_rectangles together may hold "
            f"at most {MAX_RECTANGLES} rectangles"
        )

    return {
        "zone_id": zone_id,
        "view": endpoint,
        "grid": {
            "x0": grid["x0"],
            "y0": grid["y0"],
            "cell_size": grid["cell_size"],
            "columns": columns,
            "rows": rows,
        },
        "origin_x": origin_x,
        "origin_y": origin_y,
        "cell_size": cell_size,
        "z_min_text": document["z_min"],
        "z_max_text": document["z_max"],
        "z_min": z_min,
        "z_max": z_max,
        "required_sequences": required,
        "pass_rectangles": pass_rectangles,
        "forbidden_rectangles": forbidden_rectangles,
    }


# ---------------------------------------------------------------------------
# Delivered point table (view export)


def load_view(path, endpoint):
    document = io.load(path, io.MAX_STORE_BYTES)
    fields(
        document,
        ("schema_version", "kind", "dataset_id", "version", "fingerprint", "points"),
        where="view",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported view schema_version")
    if document["kind"] != VIEW_KIND:
        raise ValueError("file is not a camera-tracks view")
    ident(document["dataset_id"], "view dataset_id")
    integer(document["version"], 0, MAX_VERSIONS, "view version")
    _digest(document["fingerprint"], "view fingerprint")
    points = {}
    for index, entry in enumerate(
        array(document["points"], 0, MAX_POINTS, "view points")
    ):
        fields(entry, ("sequence", "track_id", "xyz"), where=f"view point {index}")
        sequence = integer(
            entry["sequence"], 0, MAX_SEQUENCE, f"view point {index} sequence"
        )
        track_id = ident(entry["track_id"], f"view point {index} track_id")
        exact_array(entry["xyz"], 3, f"view point {index} xyz")
        for cell in entry["xyz"]:
            rational(cell, f"view point {index} xyz")
        key = point_key(sequence, track_id)
        if key in points:
            raise ValueError(f"duplicate point in view: {key}")
        points[key] = list(entry["xyz"])
    if fingerprint_points(points) != document["fingerprint"]:
        raise ValueError("view fingerprint does not match its point table")
    for field in ("dataset_id", "version", "fingerprint"):
        if document[field] != endpoint[field]:
            raise ValueError(
                f"view {field} {document[field]!r} does not match "
                f"the planned {endpoint[field]!r}"
            )
    return points


# ---------------------------------------------------------------------------
# Half-open integer interval algebra (sorted, disjoint, touching merged)


def _merge_intervals(intervals):
    merged = []
    for bottom, top in sorted(intervals):
        if merged and bottom <= merged[-1][1]:
            if top > merged[-1][1]:
                merged[-1][1] = top
        else:
            merged.append([bottom, top])
    return [(bottom, top) for bottom, top in merged]


def _subtract(intervals, removed):
    """``intervals`` minus ``removed``; both sorted disjoint half-open lists."""
    result = []
    j = 0
    for low, high in intervals:
        start = low
        while j < len(removed) and removed[j][1] <= start:
            j += 1
        k = j
        while k < len(removed) and removed[k][0] < high:
            cut_low, cut_high = removed[k]
            if cut_low > start:
                result.append((start, min(cut_low, high)))
            start = max(start, cut_high)
            if start >= high:
                break
            k += 1
        if start < high:
            result.append((start, high))
    return result


def _complement(intervals, rows):
    """The gaps of a sorted disjoint interval list inside [0, rows)."""
    result = []
    covered = 0
    for low, high in intervals:
        if low > covered:
            result.append((covered, low))
        covered = max(covered, high)
    if covered < rows:
        result.append((covered, rows))
    return result


def _contains(intervals, value, rows):
    """Whether any of the sorted disjoint intervals contains ``value``."""
    index = bisect.bisect_right(intervals, (value, rows)) - 1
    return index >= 0 and intervals[index][1] > value


# ---------------------------------------------------------------------------
# Elementary slab decomposition
#
# All rectangle edges are integer cell boundaries.  Cutting the grid at every
# rectangle x-edge produces vertical slabs inside which no edge starts or
# ends, so within one slab each rectangle set covers a fixed union of
# y-intervals.  The strip count is proportional to the obstacle count, never
# to the grid area.


def _slab_unions(columns, rect_sets):
    """Per slab, the merged y-interval union of each rectangle set."""
    xs = {0, columns}
    for rects in rect_sets:
        for left, _bottom, right, _top in rects:
            xs.add(left)
            xs.add(right)
    xs = sorted(xs)
    index = {x: i for i, x in enumerate(xs)}
    raw = {}
    for si, rects in enumerate(rect_sets):
        for left, bottom, right, top in rects:
            for i in range(index[left], index[right]):
                raw.setdefault((i, si), []).append((bottom, top))
    unions = {}
    for key, intervals in raw.items():
        unions[key] = _merge_intervals(intervals)
    return xs, unions


def _covered_cells(plan, points, xs, unions):
    """Cells meeting the required independent batch count, as 1x1 rectangles.

    Only points inside the grid with elevation in [z_min, z_max) count, and
    only cells outside every forbidden and passage rectangle can become
    accepted.  Coordinates are exact rationals, so the floor that assigns a
    point to its left-closed, right-open cell is computed exactly.
    """
    columns = plan["grid"]["columns"]
    rows = plan["grid"]["rows"]
    z_min = plan["z_min"]
    z_max = plan["z_max"]
    origin_x = plan["origin_x"]
    origin_y = plan["origin_y"]
    size = plan["cell_size"]
    cell_sequences = {}
    for key, xyz in points.items():
        sequence = int(key.split("/", 1)[0])
        x, y, z = (Fraction(cell) for cell in xyz)
        if not z_min <= z < z_max:
            continue
        column = (x - origin_x) // size
        row = (y - origin_y) // size
        if not (0 <= column < columns and 0 <= row < rows):
            continue
        cell_sequences.setdefault((column, row), set()).add(sequence)
    accepted = []
    for (column, row), sequences in cell_sequences.items():
        if len(sequences) < plan["required_sequences"]:
            continue
        slab = bisect.bisect_right(xs, column) - 1
        if _contains(unions.get((slab, 0), ()), row, rows):
            continue
        if _contains(unions.get((slab, 1), ()), row, rows):
            continue
        accepted.append((column, row, column + 1, row + 1))
    return accepted


class _UnionFind:
    def __init__(self):
        self.parent = []

    def add(self):
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, x):
        parent = self.parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a, b):
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_a] = root_b


# ---------------------------------------------------------------------------
# Evaluation


def build_report(plan, points):
    columns = plan["grid"]["columns"]
    rows = plan["grid"]["rows"]
    forbidden_rectangles = plan["forbidden_rectangles"]
    pass_rectangles = plan["pass_rectangles"]

    # Point coverage can only lift a cell out of the pending class; cells
    # under a forbidden or passage rectangle keep their class regardless.
    xs_cover, unions_cover = _slab_unions(
        columns, [forbidden_rectangles, pass_rectangles]
    )
    accepted = _covered_cells(plan, points, xs_cover, unions_cover)

    # Elementary strips over forbidden + passage + accepted obstacles.
    xs, unions = _slab_unions(
        columns, [forbidden_rectangles, pass_rectangles, accepted]
    )
    counts = [0, 0, 0, 0]
    components = _UnionFind()
    pending_strips = []  # (strip id, x0, x1, y0, y1)
    previous_connectable = []
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        width = x1 - x0
        forbidden = unions.get((i, 0), ())
        passage_raw = unions.get((i, 1), ())
        accepted_u = unions.get((i, 2), ())
        strips = [(y0, y1, FORBIDDEN) for y0, y1 in forbidden]
        strips += [
            (y0, y1, PASSAGE) for y0, y1 in _subtract(passage_raw, forbidden)
        ]
        strips += [(y0, y1, ACCEPTED) for y0, y1 in accepted_u]
        occupied = _merge_intervals(
            list(forbidden) + list(passage_raw) + list(accepted_u)
        )
        strips += [(y0, y1, PENDING) for y0, y1 in _complement(occupied, rows)]
        strips.sort()

        connectable = []
        for y0, y1, cls in strips:
            counts[cls] += width * (y1 - y0)
            if cls in _CONNECTABLE:
                gid = components.add()
                connectable.append((y0, y1, gid))
                if cls == PENDING:
                    pending_strips.append((gid, x0, x1, y0, y1))
        # Edge adjacency inside the slab: consecutive connectable strips
        # touch exactly when no barrier strip sits between them.
        for first, second in zip(connectable, connectable[1:]):
            if first[1] == second[0]:
                components.union(first[2], second[2])
        # Edge adjacency across the slab boundary: positive-length overlap.
        left_i, right_i = 0, 0
        while left_i < len(previous_connectable) and right_i < len(connectable):
            ly0, ly1, lg = previous_connectable[left_i]
            ry0, ry1, rg = connectable[right_i]
            if ly1 <= ry0:
                left_i += 1
            elif ry1 <= ly0:
                right_i += 1
            else:
                components.union(lg, rg)
                if ly1 < ry1:
                    left_i += 1
                elif ry1 < ly1:
                    right_i += 1
                else:
                    left_i += 1
                    right_i += 1
        previous_connectable = connectable

    total = columns * rows
    if sum(counts) != total:
        raise RuntimeError("internal error: cell classes do not tile the grid")

    # Pending strips of one connected component form one work order.
    by_component = {}
    for gid, x0, x1, y0, y1 in pending_strips:
        by_component.setdefault(components.find(gid), []).append((x0, x1, y0, y1))

    orders = []
    accounted = 0
    for strips in by_component.values():
        count = sum((x1 - x0) * (y1 - y0) for x0, x1, y0, y1 in strips)
        accounted += count
        ident_row, ident_col = min((y0, x0) for x0, x1, y0, y1 in strips)
        bounding_box = [
            min(x0 for x0, _x1, _y0, _y1 in strips),
            min(y0 for _x0, _x1, y0, _y1 in strips),
            max(x1 for _x0, x1, _y0, _y1 in strips),
            max(y1 for _x0, _x1, _y0, y1 in strips),
        ]
        # Merge strips sharing one y-interval across contiguous slabs; the
        # union over all groups stays pairwise disjoint and exactly covers
        # the order's pending cells.
        bands = {}
        for x0, x1, y0, y1 in strips:
            bands.setdefault((y0, y1), []).append((x0, x1))
        rectangles = []
        for (y0, y1), spans in bands.items():
            spans.sort()
            start, end = spans[0]
            for x0, x1 in spans[1:]:
                if x0 == end:
                    end = x1
                else:
                    rectangles.append((start, y0, end, y1))
                    start, end = x0, x1
            rectangles.append((start, y0, end, y1))
        rectangles.sort(key=lambda rect: (rect[1], rect[0], rect[3], rect[2]))
        orders.append(
            {
                "_key": (ident_row, ident_col),
                "identity": [ident_col, ident_row],
                "pending_cells": str(count),
                "bounding_box": bounding_box,
                "rectangles": [list(rect) for rect in rectangles],
            }
        )
    if accounted != counts[PENDING]:
        raise RuntimeError("internal error: pending cells lost between components")
    orders.sort(key=lambda order: order["_key"])
    for order in orders:
        del order["_key"]

    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "zone_id": plan["zone_id"],
        "view": plan["view"],
        "grid": plan["grid"],
        "z_min": plan["z_min_text"],
        "z_max": plan["z_max_text"],
        "required_sequences": plan["required_sequences"],
        "cells": {
            "accepted": str(counts[ACCEPTED]),
            "pending": str(counts[PENDING]),
            "passage": str(counts[PASSAGE]),
            "forbidden": str(counts[FORBIDDEN]),
            "total": str(total),
        },
        "orders": orders,
    }
    extra = {
        "accepted": str(counts[ACCEPTED]),
        "pending": str(counts[PENDING]),
        "passage": str(counts[PASSAGE]),
        "forbidden": str(counts[FORBIDDEN]),
        "orders": len(orders),
    }
    return report, extra


# ---------------------------------------------------------------------------
# Command entry


def run(view_path, plan_path, output_path):
    output = Path(output_path)
    inputs = {Path(p).resolve() for p in (view_path, plan_path)}
    if output.resolve() in inputs:
        raise ValueError("output path must differ from the input files")
    if output.exists() or output.is_symlink():
        raise ValueError(f"report already exists: {output_path}")
    plan = load_plan(plan_path)
    points = load_view(view_path, plan["view"])
    report, extra = build_report(plan, points)
    io.publish_new(output, io.dump_store(report))
    summary = {"zone_id": plan["zone_id"], "output": str(output_path)}
    summary.update(extra)
    return summary
