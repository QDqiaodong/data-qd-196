"""Offline earthwork (土方) accounting over two delivered point tables.

The ``earthwork`` subcommand reads two ``view`` exports — the old ground and
the new ground — and one work-zone plan, then writes one earthwork package
that a settlement system can recompute slice by slice.

Each side's ground is described by a list of rectangular blocks.  A block
names three point identities, anchoring the lower-left, lower-right and
upper-left corners of the block; the plane through those three points gives
the block's elevation everywhere inside it.  Blocks never extrapolate, and
neighbouring blocks need not agree on their shared boundary height.

The old and new ground may be partitioned differently.  Every positive-area
overlap of one old block and one new block is a candidate slice; it is
settled only when both blocks carry all three anchors and those points exist
in the corresponding view with exactly the declared corner XY.  A missing
anchor disqualifies its block and every slice that would have used it — such
a slice is never emitted, even as a zero volume.

Height difference is new Z minus old Z over the slice.  Where it is positive
the slice contributes fill, where negative it contributes cut; when a zero
line crosses the slice the two are integrated separately from the exact
plane difference, never from a net height or a centre sample.  Cut and fill
areas and volumes are exact rationals throughout; no floating point is used
for geometry or accumulation.

The sum of all settled slice areas plus the unsettled area equals the work
zone area.  Unsettled area is simply the zone area minus the settled slice
area — never the sum of the two sides' own survey gaps.  When it is positive
the package reports only settled cut/fill subtotals, never a whole-zone
earthwork total.

At most 100,000 settled slices may be delivered in one package.  A larger
settlement fails with a readable error and no package is written; slices of
different source blocks are never merged or dropped silently.

Only standard-library Python is used.  Inputs are never modified; the
package is created as one new file and an existing target, or a target
pointing at any input, is refused.
"""

import bisect
import hashlib
from fractions import Fraction
import json
from pathlib import Path

from . import io
from .checks import array, exact_array, fields, ident, integer, rational
from .state import (
    MAX_POINTS,
    MAX_SEQUENCE,
    MAX_VERSIONS,
    SCHEMA_VERSION,
    _digest,
    fingerprint_points,
    point_key,
    rational_text,
)

VIEW_KIND = "camera-tracks-view"
PLAN_KIND = "camera-tracks-earthwork-plan"
PACKAGE_KIND = "camera-tracks-earthwork"
PACKAGE_FORMAT = "camera-tracks-earthwork-v1"

MAX_BLOCKS = 20_000
MAX_SLICES = 100_000
MAX_COORDINATE = 10**9

CORNERS = ("lower_left", "lower_right", "upper_left")
SLICE_CORNERS = ("lower_left", "lower_right", "upper_right", "upper_left")
LAYERS = ("old", "new")

COMPLETE = "complete"
PARTIAL = "partial"


# ---------------------------------------------------------------------------
# Plan


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


def _area(value, where):
    cells = exact_array(value, 4, where)
    left = integer(cells[0], -MAX_COORDINATE, MAX_COORDINATE, f"{where} left")
    bottom = integer(cells[1], -MAX_COORDINATE, MAX_COORDINATE, f"{where} bottom")
    right = integer(cells[2], -MAX_COORDINATE, MAX_COORDINATE, f"{where} right")
    top = integer(cells[3], -MAX_COORDINATE, MAX_COORDINATE, f"{where} top")
    if not (left < right and bottom < top):
        raise ValueError(f"{where} must be a rectangle with positive area")
    return (left, bottom, right, top)


def _point_ref(value, where):
    fields(value, ("sequence", "track_id"), where=where)
    sequence = integer(value["sequence"], 0, MAX_SEQUENCE, f"{where} sequence")
    track_id = ident(value["track_id"], f"{where} track_id")
    return (sequence, track_id)


def _block(value, zone, layer, where):
    fields(
        value,
        ("block_id", "area", "lower_left", "lower_right", "upper_left"),
        where=where,
    )
    block_id = ident(value["block_id"], f"{where} block_id")
    left, bottom, right, top = _area(value["area"], f"{where} area")
    zl, zb, zr, zt = zone
    if not (zl <= left < right <= zr and zb <= bottom < top <= zt):
        raise ValueError(f"{where} is not inside the work zone")
    corners = {
        "lower_left": _point_ref(value["lower_left"], f"{where} lower_left"),
        "lower_right": _point_ref(value["lower_right"], f"{where} lower_right"),
        "upper_left": _point_ref(value["upper_left"], f"{where} upper_left"),
    }
    return {
        "layer": layer,
        "block_id": block_id,
        "area": (left, bottom, right, top),
        "corners": corners,
    }


def _layer(value, zone, layer, where):
    entries = array(value, 0, MAX_BLOCKS, where)
    blocks = []
    seen = set()
    for index, entry in enumerate(entries):
        block = _block(entry, zone, layer, f"{where} {index}")
        if block["block_id"] in seen:
            raise ValueError(f"{where} repeats block_id {block['block_id']!r}")
        seen.add(block["block_id"])
        blocks.append(block)
    if _overlapping_blocks(blocks):
        raise ValueError(f"{where} has overlapping block interiors")
    return blocks


def _overlapping_blocks(blocks):
    """Whether two blocks share positive area; shared edges and corners do not."""
    events = []
    for index, block in enumerate(blocks):
        left, bottom, right, top = block["area"]
        events.append((left, 1, index))
        events.append((right, 0, index))
    events.sort()
    active = []  # sorted (bottom, top, index)
    areas = [block["area"] for block in blocks]
    for _x, kind, index in events:
        _left, bottom, right, top = areas[index]
        if kind == 0:  # removal
            position = bisect.bisect_left(active, (bottom,))
            while position < len(active) and active[position][2] != index:
                position += 1
            if position >= len(active):
                raise RuntimeError("internal error: block missing from sweep")
            active.pop(position)
        else:  # insertion
            position = bisect.bisect_left(active, (bottom,))
            if position and active[position - 1][1] > bottom:
                return True
            if position < len(active) and active[position][0] < top:
                return True
            active.insert(position, (bottom, top, index))
    return False


def load_plan(path):
    document = io.load(path, io.MAX_STORE_BYTES)
    fields(
        document,
        ("schema_version", "kind", "zone_id", "zone", "old", "new"),
        where="earthwork plan",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported earthwork plan schema_version")
    if document["kind"] != PLAN_KIND:
        raise ValueError("not a camera-tracks earthwork plan")
    zone_id = ident(document["zone_id"], "zone_id")
    zone = _area(document["zone"], "zone")
    plan = {"zone_id": zone_id, "zone": zone}
    for layer in LAYERS:
        side = fields(
            document[layer],
            ("dataset_id", "version", "fingerprint", "blocks"),
            where=f"plan {layer}",
        )
        plan[layer] = _endpoint(
            {
                "dataset_id": side["dataset_id"],
                "version": side["version"],
                "fingerprint": side["fingerprint"],
            },
            f"plan {layer}",
        )
        plan[f"{layer}_blocks"] = _layer(
            side["blocks"], zone, layer, f"{layer}_blocks"
        )
    return plan


# ---------------------------------------------------------------------------
# Delivered point tables (view exports)


def load_view(path, endpoint, role):
    document = io.load(path, io.MAX_STORE_BYTES)
    fields(
        document,
        ("schema_version", "kind", "dataset_id", "version", "fingerprint", "points"),
        where=f"{role} view",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported {role} view schema_version")
    if document["kind"] != VIEW_KIND:
        raise ValueError(f"{role} file is not a camera-tracks view")
    ident(document["dataset_id"], f"{role} view dataset_id")
    integer(document["version"], 0, MAX_VERSIONS, f"{role} view version")
    _digest(document["fingerprint"], f"{role} view fingerprint")
    raw = {}
    for index, entry in enumerate(
        array(document["points"], 0, MAX_POINTS, f"{role} view points")
    ):
        fields(
            entry, ("sequence", "track_id", "xyz"), where=f"{role} view point {index}"
        )
        sequence = integer(
            entry["sequence"], 0, MAX_SEQUENCE, f"{role} view point {index} sequence"
        )
        track_id = ident(entry["track_id"], f"{role} view point {index} track_id")
        exact_array(entry["xyz"], 3, f"{role} view point {index} xyz")
        for cell in entry["xyz"]:
            rational(cell, f"{role} view point {index} xyz")
        key = point_key(sequence, track_id)
        if key in raw:
            raise ValueError(f"duplicate point in {role} view: {key}")
        raw[key] = list(entry["xyz"])
    if fingerprint_points(raw) != document["fingerprint"]:
        raise ValueError(f"{role} view fingerprint does not match its point table")
    for field in ("dataset_id", "version", "fingerprint"):
        if document[field] != endpoint[field]:
            raise ValueError(
                f"{role} view {field} {document[field]!r} does not match "
                f"the planned {endpoint[field]!r}"
            )
    return {
        key: tuple(Fraction(cell) for cell in xyz) for key, xyz in raw.items()
    }


# ---------------------------------------------------------------------------
# Anchor resolution and elevation planes


def _resolve(blocks, points):
    """Split blocks into resolvable planes and missing-anchor evidence."""
    planes = []
    missing = []
    for block in blocks:
        left, bottom, right, top = block["area"]
        expected = {
            "lower_left": (left, bottom),
            "lower_right": (right, bottom),
            "upper_left": (left, top),
        }
        heights = {}
        absent = []
        for corner in CORNERS:
            key = point_key(*block["corners"][corner])
            xyz = points.get(key)
            if xyz is None:
                absent.append(corner)
                continue
            ex, ey = expected[corner]
            if xyz[0] != ex or xyz[1] != ey:
                raise ValueError(
                    f"{block['layer']} block {block['block_id']!r} {corner} "
                    f"anchor {key!r} has XY "
                    f"({rational_text(xyz[0])}, {rational_text(xyz[1])}) but the "
                    f"declared corner is ({ex}, {ey})"
                )
            heights[corner] = xyz[2]
        if absent:
            missing.append(
                {
                    "layer": block["layer"],
                    "block_id": block["block_id"],
                    "area": list(block["area"]),
                    "missing_anchors": [
                        {
                            "corner": corner,
                            "sequence": block["corners"][corner][0],
                            "track_id": block["corners"][corner][1],
                        }
                        for corner in absent
                    ],
                }
            )
            continue
        planes.append(_block_plane(block, heights))
    return planes, missing


def _block_plane(block, heights):
    left, bottom, right, top = block["area"]
    base = heights["lower_left"]
    dx = Fraction(right - left)
    dy = Fraction(top - bottom)
    a = (heights["lower_right"] - base) / dx
    b = (heights["upper_left"] - base) / dy
    c = base - a * left - b * bottom
    return {
        "layer": block["layer"],
        "block_id": block["block_id"],
        "area": block["area"],
        "plane": (a, b, c),
    }


# ---------------------------------------------------------------------------
# Slice geometry: an affine height difference over a rectangle


def _cut_fill(difference, rectangle):
    """Exact ``(cut, fill)`` over one rectangle for affine ``difference``."""
    left, bottom, right, top = rectangle
    a, b, c = difference

    def value(x, y):
        return a * x + b * y + c

    corners = [
        (left, bottom),
        (right, bottom),
        (right, top),
        (left, top),
    ]
    signed = [value(x, y) for x, y in corners]
    area = Fraction((right - left) * (top - bottom))
    centre = (Fraction(left + right, 2), Fraction(bottom + top, 2))
    if all(item >= 0 for item in signed):
        # No negative part; the affine integral equals area times the value
        # at the centre.  This also covers slices of exactly equal height.
        return Fraction(0), area * value(*centre)
    if all(item <= 0 for item in signed):
        return -area * value(*centre), Fraction(0)
    polygon = [(Fraction(x), Fraction(y)) for x, y in corners]
    fill = _polygon_integral(_clip(polygon, difference, 1), difference)
    cut = -_polygon_integral(_clip(polygon, difference, -1), difference)
    if cut < 0 or fill < 0:
        raise RuntimeError("internal error: negative cut or fill on a slice")
    return cut, fill


def _clip(polygon, difference, sign):
    """Sutherland-Hodgman clip of a CCW polygon to ``sign * difference >= 0``."""
    a, b, c = difference

    def value(point):
        return sign * (a * point[0] + b * point[1] + c)

    result = []
    count = len(polygon)
    for index in range(count):
        current = polygon[index]
        previous = polygon[index - 1]
        current_value = value(current)
        previous_value = value(previous)
        if current_value >= 0:
            if previous_value < 0:
                result.append(
                    _crossing(previous, current, previous_value, current_value)
                )
            result.append(current)
        elif previous_value >= 0:
            result.append(
                _crossing(previous, current, previous_value, current_value)
            )
    return result


def _crossing(start, end, start_value, end_value):
    ratio = start_value / (start_value - end_value)
    return (
        start[0] + ratio * (end[0] - start[0]),
        start[1] + ratio * (end[1] - start[1]),
    )


def _polygon_integral(polygon, difference):
    """Exact integral of an affine function over a CCW polygon."""
    count = len(polygon)
    if count < 3:
        return Fraction(0)
    twice_area = Fraction(0)
    moment_x = Fraction(0)
    moment_y = Fraction(0)
    for index in range(count):
        x0, y0 = polygon[index]
        x1, y1 = polygon[(index + 1) % count]
        cross = x0 * y1 - x1 * y0
        twice_area += cross
        moment_x += (x0 + x1) * cross
        moment_y += (y0 + y1) * cross
    if twice_area == 0:
        return Fraction(0)
    if twice_area < 0:
        # Clipping preserves orientation, but be defensive for exact science.
        twice_area = -twice_area
        moment_x = -moment_x
        moment_y = -moment_y
    centre_x = moment_x / (3 * twice_area)
    centre_y = moment_y / (3 * twice_area)
    a, b, c = difference
    return (twice_area / 2) * (a * centre_x + b * centre_y + c)


# ---------------------------------------------------------------------------
# Candidate slices


def _intersections(old_blocks, new_blocks):
    """All positive-area old/new block overlaps, in a deterministic order.

    Sweep the x axis over both layers' left and right edges.  Within one
    layer the active blocks are pairwise disjoint, so their y-intervals can
    be kept in one bottom-sorted list and every crossing candidate found by
    binary search.  Each overlapping pair is reported once, when the block
    with the later left edge is inserted; removals sort before insertions at
    a shared edge, so blocks that only touch are never paired.
    """
    layers = (old_blocks, new_blocks)
    events = []
    for layer_index, blocks in enumerate(layers):
        for index, block in enumerate(blocks):
            left, _bottom, right, _top = block["area"]
            events.append((left, 1, layer_index, index))  # add at left edge
            events.append((right, 0, layer_index, index))  # remove at right
    events.sort()
    active = ([], [])
    slices = []
    for _x, kind, layer_index, index in events:
        block = layers[layer_index][index]
        _left, bottom, _right, top = block["area"]
        mine = active[layer_index]
        others = active[1 - layer_index]
        position = bisect.bisect_left(mine, (bottom,))
        if kind == 0:
            while position < len(mine) and mine[position][2] != index:
                position += 1
            if position >= len(mine):
                raise RuntimeError("internal error: block missing from sweep")
            mine.pop(position)
            continue
        candidates = []
        if position and mine[position - 1][1] > bottom:
            raise RuntimeError("internal error: same-layer block overlap")
        mine.insert(position, (bottom, top, index))
        # Blocks of the other layer overlapping [bottom, top) in y.
        start = bisect.bisect_left(others, (bottom,))
        if start and others[start - 1][1] > bottom:
            candidates.append(others[start - 1][2])
        scan = start
        while scan < len(others) and others[scan][0] < top:
            candidates.append(others[scan][2])
            scan += 1
        for other_index in candidates:
            other = layers[1 - layer_index][other_index]
            o_left, o_bottom, o_right, o_top = other["area"]
            left = max(_left, o_left)
            below = max(bottom, o_bottom)
            right = min(_right, o_right)
            above = min(top, o_top)
            if left < right and below < above:
                old_index, new_index = (
                    (index, other_index)
                    if layer_index == 0
                    else (other_index, index)
                )
                slices.append(
                    (
                        left,
                        below,
                        right,
                        above,
                        old_blocks[old_index]["block_id"],
                        new_blocks[new_index]["block_id"],
                        old_index,
                        new_index,
                    )
                )
                if len(slices) > MAX_SLICES:
                    raise ValueError(
                        f"earthwork settlement has more than {MAX_SLICES} "
                        "settled slices; no package was written"
                    )
    slices.sort(key=lambda item: item[:6])
    return slices


# ---------------------------------------------------------------------------
# Package


def build_package(plan, old_points, new_points):
    old_planes, old_missing = _resolve(plan["old_blocks"], old_points)
    new_planes, new_missing = _resolve(plan["new_blocks"], new_points)
    missing = old_missing + new_missing
    missing.sort(key=lambda entry: (LAYERS.index(entry["layer"]), entry["block_id"]))

    slices = _intersections(old_planes, new_planes)
    zone_left, zone_bottom, zone_right, zone_top = plan["zone"]
    zone_area = (zone_right - zone_left) * (zone_top - zone_bottom)
    return old_planes, new_planes, missing, slices, zone_area


# ---------------------------------------------------------------------------
# Rendering


def _package_digest(plan, slices, missing):
    """SHA-256 over the canonical settlement, independent of input order."""
    digest = hashlib.sha256()
    digest.update(PACKAGE_FORMAT.encode("ascii") + b"\n")
    digest.update(_dump({
        "zone_id": plan["zone_id"],
        "zone": list(plan["zone"]),
        "old": plan["old"],
        "new": plan["new"],
    }))
    for item in slices:
        digest.update(b"\n")
        digest.update(_dump(list(item[:6])))
    for entry in missing:
        digest.update(b"\n")
        digest.update(_dump(entry))
    return digest.hexdigest()


def _dump(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _slice_document(item, old_planes, new_planes):
    left, bottom, right, top, _old_id, _new_id, old_index, new_index = item
    old = old_planes[old_index]
    new = new_planes[new_index]
    difference = tuple(
        new["plane"][index] - old["plane"][index] for index in range(3)
    )
    a, b, c = difference

    def value(x, y):
        return a * x + b * y + c

    corners = {
        "lower_left": value(left, bottom),
        "lower_right": value(right, bottom),
        "upper_right": value(right, top),
        "upper_left": value(left, top),
    }
    cut, fill = _cut_fill(difference, (left, bottom, right, top))
    return {
        "range": [left, bottom, right, top],
        "old_block": old["block_id"],
        "new_block": new["block_id"],
        "corner_differences": {
            corner: rational_text(corners[corner]) for corner in SLICE_CORNERS
        },
        "cut": rational_text(cut),
        "fill": rational_text(fill),
    }, cut, fill


def write_package(stream, plan, old_planes, new_planes, missing, slices, zone_area):
    """Stream the package deterministically; return the printed summary."""
    write = stream.write
    header = {
        "schema_version": SCHEMA_VERSION,
        "kind": PACKAGE_KIND,
        "zone_id": plan["zone_id"],
        "zone": list(plan["zone"]),
        "zone_area": str(zone_area),
        "old": plan["old"],
        "new": plan["new"],
        "slice_limit": MAX_SLICES,
        "slice_count": len(slices),
        "package_digest": PACKAGE_FORMAT + ":" + _package_digest(
            plan, slices, missing
        ),
    }
    write(b"{")
    for index, (key, value) in enumerate(header.items()):
        if index:
            write(b",")
        write(_dump(key))
        write(b":")
        write(_dump(value))
    write(b',"slices":[')
    settled_area = 0
    total_cut = Fraction(0)
    total_fill = Fraction(0)
    for index, item in enumerate(slices):
        document, cut, fill = _slice_document(item, old_planes, new_planes)
        if index:
            write(b",")
        write(_dump(document))
        settled_area += (item[2] - item[0]) * (item[3] - item[1])
        total_cut += cut
        total_fill += fill
    write(b'],"missing_blocks":')
    write(_dump(missing))
    unsettled_area = zone_area - settled_area
    if unsettled_area < 0:
        raise RuntimeError("internal error: settled area exceeds the work zone")
    tail = {
        "settled_area": str(settled_area),
        "unsettled_area": str(unsettled_area),
        "settled_cut": rational_text(total_cut),
        "settled_fill": rational_text(total_fill),
        "status": COMPLETE if unsettled_area == 0 else PARTIAL,
    }
    for key, value in tail.items():
        write(b",")
        write(_dump(key))
        write(b":")
        write(_dump(value))
    write(b"}\n")
    summary = {
        "zone_id": plan["zone_id"],
        "slices": len(slices),
        "settled_area": str(settled_area),
        "unsettled_area": str(unsettled_area),
        "settled_cut": rational_text(total_cut),
        "settled_fill": rational_text(total_fill),
        "status": tail["status"],
        "missing_blocks": len(missing),
    }
    return summary


# ---------------------------------------------------------------------------
# Command entry


def run(old_path, new_path, plan_path, output_path):
    output = Path(output_path)
    inputs = {Path(p).resolve() for p in (old_path, new_path, plan_path)}
    if output.resolve() in inputs:
        raise ValueError("output path must differ from the input files")
    if output.exists() or output.is_symlink():
        raise ValueError(f"package already exists: {output_path}")
    plan = load_plan(plan_path)
    old_points = load_view(old_path, plan["old"], "old")
    new_points = load_view(new_path, plan["new"], "new")
    old_planes, new_planes, missing, slices, zone_area = build_package(
        plan, old_points, new_points
    )
    holder = {}

    def write(stream):
        holder["summary"] = write_package(
            stream, plan, old_planes, new_planes, missing, slices, zone_area
        )

    # Every failure inside the writer (including the slice limit) happens
    # before the hard link, so the target never holds a partial package.
    io.publish_new_stream(output, write)
    summary = holder["summary"]
    summary["output"] = str(output_path)
    return summary
