"""Offline origin merge (坐标归并) for independently based point-cloud views.

A construction survey is delivered as several fragment views.  Each view uses
metres and shares the engineering coordinate axes, but every fragment has its
own local origin T(u): a point with local XYZ is at XYZ + T(u) in engineering
coordinates.  The baseline fragment has T = 0.

A linkage plan records survey ties between fragment origins.  A tie u -> v
measures T(v) - T(u) as three integer micron components; using it backwards
negates every component.  Ties are either reviewed (never discardable) or
tentative (may be the single faulty record).

The network is solved exactly with integer micron arithmetic and a BFS
spanning tree.  When the ties disagree, every tentative record is considered
as the *single* discarded record: discarding it must leave the graph connected
to the baseline and admit one exact position for every fragment.  No fitting,
no two-record repairs, no preference for the first candidate.  A fragment is
released only when every feasible single-discard interpretation gives it the
same T; otherwise two recomputable discard evidences with distinct positions
are reported.  When no single tentative discard explains the batch, an
explicitly unexplained package is written without any merged point.

Only the standard library is used.  Inputs and existing state are read only;
the package is one new file and an existing target, or a target pointing at
any input, is refused.
"""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

from . import io
from .checks import array, exact_array, fields, ident, integer, rational
from .faults import MERGE_BEFORE_WRITE, checkpoint
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

PLAN_KIND = "camera-tracks-merge-plan"
PACKAGE_KIND = "camera-tracks-merge-package"
PACKAGE_FORMAT = "camera-tracks-merge-v1"

MAX_FRAGMENTS = 60_000
MAX_LINKS = 120_000
MAX_COMPONENT_UM = 10**12
MAX_PLAN_BYTES = 64 * 1024 * 1024
MICRONS_PER_METRE = 1_000_000

VIEW_KIND = "camera-tracks-view"
REVIEWED = "reviewed"
TENTATIVE = "tentative"

# Package decisions.
RELEASED = "released"
WITHHELD = "withheld"
NOT_RELEASED = "not_released"

# Package statuses.
CONSISTENT = "consistent"
RESOLVED = "resolved"
UNEXPLAINED = "unexplained"


# ---------------------------------------------------------------------------
# Plan validation
#


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


def load_plan(path):
    document = io.load(path, MAX_PLAN_BYTES)
    fields(
        document,
        ("schema_version", "kind", "plan_id", "baseline", "fragments", "links"),
        where="merge plan",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported merge plan schema_version")
    if document["kind"] != PLAN_KIND:
        raise ValueError("not a camera-tracks merge plan")
    plan_id = ident(document["plan_id"], "plan_id")
    baseline = ident(document["baseline"], "baseline")

    fragment_docs = array(
        document["fragments"], 1, MAX_FRAGMENTS, "merge plan fragments"
    )
    index_by_id = {}
    fragments = []
    endpoints = {}
    for position, entry in enumerate(fragment_docs):
        fields(entry, ("fragment_id", "endpoint"), where=f"fragment {position}")
        fragment_id = ident(entry["fragment_id"], "fragment_id")
        if fragment_id in index_by_id:
            raise ValueError(f"duplicate fragment id: {fragment_id}")
        index_by_id[fragment_id] = len(fragments)
        endpoint = None
        if entry["endpoint"] is not None:
            endpoint = _endpoint(entry["endpoint"], f"fragment {fragment_id} endpoint")
            endpoints[fragment_id] = endpoint
        fragments.append({"fragment_id": fragment_id, "endpoint": endpoint})
    if baseline not in index_by_id:
        raise ValueError(f"baseline fragment {baseline!r} is not declared")

    link_docs = array(document["links"], 0, MAX_LINKS, "merge plan links")
    record_ids = set()
    edge_count = len(link_docs)
    from array import array as _typed_array

    edge_record = [None] * edge_count
    edge_u = _typed_array("i", [0]) * edge_count
    edge_v = _typed_array("i", [0]) * edge_count
    edge_dx = _typed_array("q", [0]) * edge_count
    edge_dy = _typed_array("q", [0]) * edge_count
    edge_dz = _typed_array("q", [0]) * edge_count
    edge_tentative = bytearray(edge_count)
    # Build the undirected adjacency in CSR form: pairs of (neighbour, edge,
    # sign) appended per source node, then flattened with an offset table.
    adjacency_lists = [[] for _ in fragments]
    for position, entry in enumerate(link_docs):
        fields(
            entry,
            ("record_id", "from_fragment", "to_fragment", "delta", "status"),
            where=f"link {position}",
        )
        record_id = ident(entry["record_id"], "link record_id")
        if record_id in record_ids:
            raise ValueError(f"duplicate link record id: {record_id}")
        record_ids.add(record_id)
        source = ident(entry["from_fragment"], "link from_fragment")
        target = ident(entry["to_fragment"], "link to_fragment")
        if source not in index_by_id:
            raise ValueError(f"link {record_id} references unknown fragment: {source}")
        if target not in index_by_id:
            raise ValueError(f"link {record_id} references unknown fragment: {target}")
        if source == target:
            raise ValueError(f"link {record_id} must join two different fragments")
        delta = exact_array(entry["delta"], 3, f"link {record_id} delta")
        components = []
        for axis, cell in enumerate(delta):
            components.append(
                integer(
                    cell,
                    -MAX_COMPONENT_UM,
                    MAX_COMPONENT_UM,
                    f"link {record_id} delta[{axis}]",
                )
            )
        if entry["status"] not in (REVIEWED, TENTATIVE):
            raise ValueError(
                f"link {record_id} status must be 'reviewed' or 'tentative'"
            )
        edge_index = position
        u = index_by_id[source]
        v = index_by_id[target]
        edge_record[edge_index] = record_id
        edge_u[edge_index] = u
        edge_v[edge_index] = v
        edge_dx[edge_index] = components[0]
        edge_dy[edge_index] = components[1]
        edge_dz[edge_index] = components[2]
        if entry["status"] == TENTATIVE:
            edge_tentative[edge_index] = 1
        # adjacency[u] entry (v, edge, sign): T(v) - T(u) == sign * delta.
        adjacency_lists[u].append((v, edge_index, 1))
        adjacency_lists[v].append((u, edge_index, -1))

    offset = _typed_array("i", [0]) * (len(fragments) + 1)
    total_entries = 0
    for node, entries in enumerate(adjacency_lists):
        offset[node] = total_entries
        total_entries += len(entries)
    offset[len(fragments)] = total_entries
    neighbour_node = _typed_array("i", [0]) * total_entries
    neighbour_edge = _typed_array("i", [0]) * total_entries
    neighbour_sign = bytearray(total_entries)  # 1 -> +1, 255 -> -1
    for node, entries in enumerate(adjacency_lists):
        cursor = offset[node]
        for v, edge_index, sign in entries:
            neighbour_node[cursor] = v
            neighbour_edge[cursor] = edge_index
            neighbour_sign[cursor] = 1 if sign == 1 else 255
            cursor += 1
    adjacency = {
        "offset": offset,
        "node": neighbour_node,
        "edge": neighbour_edge,
        "sign": neighbour_sign,
    }
    del adjacency_lists

    if not _connected(adjacency, index_by_id[baseline]):
        raise ValueError("merge plan links do not connect every fragment to the baseline")

    return {
        "plan_id": plan_id,
        "baseline": baseline,
        "baseline_index": index_by_id[baseline],
        "fragments": fragments,
        "index_by_id": index_by_id,
        "endpoints": endpoints,
        "edge_record": edge_record,
        "edge_u": edge_u,
        "edge_v": edge_v,
        "edge_dx": edge_dx,
        "edge_dy": edge_dy,
        "edge_dz": edge_dz,
        "edge_tentative": edge_tentative,
        "adjacency": adjacency,
    }


def _iter_neighbours(adjacency, node):
    offset = adjacency["offset"]
    start = offset[node]
    end = offset[node + 1]
    nodes = adjacency["node"]
    edges = adjacency["edge"]
    signs = adjacency["sign"]
    for cursor in range(start, end):
        sign = signs[cursor]
        yield nodes[cursor], edges[cursor], 1 if sign == 1 else -1


def _connected(adjacency, baseline):
    fragment_count = len(adjacency["offset"]) - 1
    seen = bytearray(fragment_count)
    seen[baseline] = 1
    found = 1
    stack = [baseline]
    while stack:
        u = stack.pop()
        for v, _edge, _sign in _iter_neighbours(adjacency, u):
            if not seen[v]:
                seen[v] = 1
                found += 1
                stack.append(v)
    return found == fragment_count


# ---------------------------------------------------------------------------
# View inputs
#


def load_view(path, endpoint, fragment_id):
    """Validate one bound view against its planned endpoint.

    The point-table fingerprint is always recomputed and checked, even when
    the fragment will later be withheld, and all three endpoint fields must
    match: an equal fingerprint cannot substitute for another version.
    """
    document = io.load(path, io.MAX_STORE_BYTES)
    fields(
        document,
        ("schema_version", "kind", "dataset_id", "version", "fingerprint", "points"),
        where=f"fragment {fragment_id} view",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"fragment {fragment_id} view has unsupported schema_version")
    if document["kind"] != VIEW_KIND:
        raise ValueError(f"fragment {fragment_id} input is not a camera-tracks view")
    ident(document["dataset_id"], f"fragment {fragment_id} view dataset_id")
    integer(
        document["version"], 0, MAX_VERSIONS, f"fragment {fragment_id} view version"
    )
    _digest(document["fingerprint"], f"fragment {fragment_id} view fingerprint")
    table = {}
    rows = []
    for position, entry in enumerate(
        array(document["points"], 0, MAX_POINTS, f"fragment {fragment_id} view points")
    ):
        fields(
            entry,
            ("sequence", "track_id", "xyz"),
            where=f"fragment {fragment_id} view point {position}",
        )
        sequence = integer(
            entry["sequence"],
            0,
            MAX_SEQUENCE,
            f"fragment {fragment_id} view point {position} sequence",
        )
        track_id = ident(
            entry["track_id"], f"fragment {fragment_id} view point {position} track_id"
        )
        exact_array(entry["xyz"], 3, f"fragment {fragment_id} view point {position} xyz")
        for cell in entry["xyz"]:
            rational(cell, f"fragment {fragment_id} view point {position} xyz")
        key = point_key(sequence, track_id)
        if key in table:
            raise ValueError(
                f"fragment {fragment_id} view repeats point identity: {key}"
            )
        table[key] = list(entry["xyz"])
        rows.append(
            {
                "sequence": sequence,
                "track_id": track_id,
                "xyz": list(entry["xyz"]),
            }
        )
    if fingerprint_points(table) != document["fingerprint"]:
        raise ValueError(
            f"fragment {fragment_id} view fingerprint does not match its point table"
        )
    for field in ("dataset_id", "version", "fingerprint"):
        if document[field] != endpoint[field]:
            raise ValueError(
                f"fragment {fragment_id} view {field} {document[field]!r} does not "
                f"match the planned endpoint {endpoint[field]!r}"
            )
    return rows


def resolve_inputs(plan, input_map):
    """Bind one existing view file to every endpoint fragment, no more, no less."""
    bound = set(plan["endpoints"])
    supplied = set(input_map)
    missing = sorted(bound - supplied)
    extra = sorted(supplied - bound)
    if missing:
        raise ValueError(f"missing view input for fragment(s): {', '.join(missing)}")
    if extra:
        raise ValueError(
            "view input(s) supplied for fragment(s) without a planned endpoint: "
            + ", ".join(extra)
        )
    views = {}
    # Validate every bound view before any network decision, so tampering with
    # a subsequently withheld fragment is still rejected as an input error.
    for fragment_id in sorted(bound):
        views[fragment_id] = load_view(
            input_map[fragment_id], plan["endpoints"][fragment_id], fragment_id
        )
    return views


# ---------------------------------------------------------------------------
# Exact network analysis (integer microns)
#


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _neg(a):
    return (-a[0], -a[1], -a[2])


def spanning_tree(plan):
    """BFS spanning tree from the baseline; returns tree positions in microns."""
    from array import array as _typed_array

    n = len(plan["fragments"])
    baseline = plan["baseline_index"]
    adjacency = plan["adjacency"]
    dx = plan["edge_dx"]
    dy = plan["edge_dy"]
    dz = plan["edge_dz"]
    parent_edge = _typed_array("i", [-1]) * n
    parent_node = _typed_array("i", [-1]) * n
    pos_x = _typed_array("q", [0]) * n
    pos_y = _typed_array("q", [0]) * n
    pos_z = _typed_array("q", [0]) * n
    seen = bytearray(n)
    seen[baseline] = 1
    order = _typed_array("i", [0]) * n
    order[0] = baseline
    head = 0
    count = 1
    while head < count:
        u = order[head]
        head += 1
        for v, edge_index, sign in _iter_neighbours(adjacency, u):
            if seen[v]:
                continue
            seen[v] = 1
            parent_edge[v] = edge_index
            parent_node[v] = u
            pos_x[v] = pos_x[u] + sign * dx[edge_index]
            pos_y[v] = pos_y[u] + sign * dy[edge_index]
            pos_z[v] = pos_z[u] + sign * dz[edge_index]
            order[count] = v
            count += 1
    if count != n:  # connectivity was already checked; stay defensive.
        raise RuntimeError("internal error: linkage graph is not connected")
    return parent_edge, parent_node, (pos_x, pos_y, pos_z), order


def _residual_at(plan, edge_index, positions):
    """Residual r = T(v) - T(u) - delta for the record orientation u -> v."""
    px, py, pz = positions
    edge_u = plan["edge_u"][edge_index]
    edge_v = plan["edge_v"][edge_index]
    return (
        px[edge_v] - px[edge_u] - plan["edge_dx"][edge_index],
        py[edge_v] - py[edge_u] - plan["edge_dy"][edge_index],
        pz[edge_v] - pz[edge_u] - plan["edge_dz"][edge_index],
    )


def _subtree_intervals(parent_node):
    """Subtree preorder intervals via an iterative DFS of the spanning tree."""
    from array import array as _typed_array

    n = len(parent_node)
    root = next(node for node in range(n) if parent_node[node] < 0)
    # First-child/next-sibling representation avoids one list per fragment.
    first_child = _typed_array("i", [-1]) * n
    next_sibling = _typed_array("i", [-1]) * n
    for node in range(n):
        parent = parent_node[node]
        if parent >= 0:
            next_sibling[node] = first_child[parent]
            first_child[parent] = node
    tin = _typed_array("i", [0]) * n
    tout = _typed_array("i", [0]) * n
    clock = 0
    # Each stack entry is (node, entering); exit frames carry node + n so no
    # tuple allocation is needed.
    stack = [root]
    while stack:
        value = stack.pop()
        if value < n:
            node = value
            tin[node] = clock
            clock += 1
            stack.append(node + n)
            child = first_child[node]
            # Push siblings in reverse so the smallest child is visited first.
            while child >= 0:
                stack.append(child)
                child = next_sibling[child]
        else:
            tout[value - n] = clock - 1
    if clock != n:
        raise RuntimeError("internal error: spanning tree walk missed a fragment")
    return tin, tout


def _in_subtree(node, child, tin, tout):
    return tin[child] <= tin[node] <= tout[child]


def _coverage(edge_indices, plan, parent_node, order, tin, tout):
    """Crossing counts per tree edge without an LCA table.

    Each non-tree edge contributes +1 to both endpoints and -2 at the highest
    ancestor inside its path; accumulating from BFS leaves upwards yields the
    number of processed edges crossing every parent->child tree edge.
    """
    from array import array as _typed_array

    n = len(parent_node)
    diff = _typed_array("i", [0]) * n
    edge_u = plan["edge_u"]
    edge_v = plan["edge_v"]
    for edge_index in edge_indices:
        u = edge_u[edge_index]
        v = edge_v[edge_index]
        diff[u] += 1
        diff[v] += 1
        # Walk v up to the LCA(u, v) using subtree intervals, subtracting 2
        # once at that ancestor.  BFS guarantees the LCA sits above both ends.
        ancestor = v
        while not _in_subtree(u, ancestor, tin, tout):
            ancestor = parent_node[ancestor]
            if ancestor < 0:
                raise RuntimeError("internal error: LCA walk left the tree")
        diff[ancestor] -= 2
    for cursor in range(n - 1, -1, -1):
        node = order[cursor]
        parent = parent_node[node]
        if parent >= 0:
            diff[parent] += diff[node]
    return diff


def analyze(plan):
    """Return the exact feasibility structure of the linkage network.

    Feasible single-discard candidates are kept in parallel typed arrays
    (edge index, shifted subtree root with -1 for non-tree edges, and the
    three shift components in microns) rather than per-candidate dicts, so a
    20,000-candidate batch stays well inside the memory budget.
    """
    from array import array as _typed_array

    edge_count = len(plan["edge_record"])
    n = len(plan["fragments"])
    parent_edge, parent_node, positions, order = spanning_tree(plan)
    tree_edge_marker = bytearray(edge_count)
    for edge_index in parent_edge:
        if edge_index >= 0:
            tree_edge_marker[edge_index] = 1

    non_tree = [edge_index for edge_index in range(edge_count)
                if not tree_edge_marker[edge_index]]
    residuals = {}
    bad = []
    zero_edges = []
    for edge_index in non_tree:
        residual = _residual_at(plan, edge_index, positions)
        residuals[edge_index] = residual
        if residual != (0, 0, 0):
            bad.append(edge_index)
        else:
            zero_edges.append(edge_index)

    cand_edge = _typed_array("i")
    cand_root = _typed_array("i")
    cand_sx = _typed_array("q")
    cand_sy = _typed_array("q")
    cand_sz = _typed_array("q")

    if not bad:
        return {
            "status": CONSISTENT,
            "positions": positions,
            "cand_edge": cand_edge,
            "cand_root": cand_root,
            "cand_sx": cand_sx,
            "cand_sy": cand_sy,
            "cand_sz": cand_sz,
            "parent_edge": parent_edge,
            "parent_node": parent_node,
            "order": order,
        }

    tin, tout = _subtree_intervals(parent_node)
    bad_cover = _coverage(bad, plan, parent_node, order, tin, tout)
    zero_cover = _coverage(zero_edges, plan, parent_node, order, tin, tout)

    def add_candidate(edge_index, subtree_root, shift):
        cand_edge.append(edge_index)
        cand_root.append(subtree_root)
        cand_sx.append(shift[0])
        cand_sy.append(shift[1])
        cand_sz.append(shift[2])

    # A discarded non-tree edge leaves the spanning tree (hence connectivity)
    # intact; it works only when every other non-tree residual is already zero.
    if len(bad) == 1:
        sole = bad[0]
        if plan["edge_tentative"][sole]:
            add_candidate(sole, -1, (0, 0, 0))

    edge_u = plan["edge_u"]
    edge_v = plan["edge_v"]
    # A discarded tree edge splits off one subtree.  Every bad non-tree edge
    # must cross that cut, no zero-residual edge may cross it (it would force
    # a zero shift while a bad edge forces a nonzero one), and the crossing
    # bad edges must all demand the same shift.
    for child in range(n):
        edge_index = parent_edge[child]
        if edge_index < 0 or not plan["edge_tentative"][edge_index]:
            continue
        if bad_cover[child] != len(bad) or zero_cover[child] != 0:
            continue
        shift = None
        ok = True
        for bad_index in bad:
            residual = residuals[bad_index]
            u_inside = _in_subtree(edge_u[bad_index], child, tin, tout)
            v_inside = _in_subtree(edge_v[bad_index], child, tin, tout)
            if u_inside == v_inside:
                ok = False  # bad_cover promised a crossing; keep defensive.
                break
            if v_inside:
                required = _neg(residual)  # record points L -> R
            else:
                required = residual        # record points R -> L
            if shift is None:
                shift = required
            elif shift != required:
                ok = False
                break
        if ok and shift is not None:
            add_candidate(edge_index, child, shift)

    status = UNEXPLAINED if not cand_edge else RESOLVED
    return {
        "status": status,
        "positions": positions,
        "cand_edge": cand_edge,
        "cand_root": cand_root,
        "cand_sx": cand_sx,
        "cand_sy": cand_sy,
        "cand_sz": cand_sz,
        "parent_edge": parent_edge,
        "parent_node": parent_node,
        "order": order,
        "tin": tin,
        "tout": tout,
        "bad": bad,
    }


def solve_without(plan, discarded_edge):
    """Independent BFS re-solve with one record removed; used as a cross-check.

    Returns three parallel signed 64-bit micron arrays.  Raises ValueError
    when the remaining graph disconnects or cannot satisfy every tie exactly.
    """
    from array import array as _typed_array

    adjacency = plan["adjacency"]
    baseline = plan["baseline_index"]
    n = len(plan["fragments"])
    dx = plan["edge_dx"]
    dy = plan["edge_dy"]
    dz = plan["edge_dz"]
    unseen = bytearray(b"\x01") * n
    pos_x = _typed_array("q", [0]) * n
    pos_y = _typed_array("q", [0]) * n
    pos_z = _typed_array("q", [0]) * n
    unseen[baseline] = 0
    queue = _typed_array("i", [0]) * n
    queue[0] = baseline
    head = 0
    count = 1
    while head < count:
        u = queue[head]
        head += 1
        for v, edge_index, sign in _iter_neighbours(adjacency, u):
            if edge_index == discarded_edge:
                continue
            predicted = (
                pos_x[u] + sign * dx[edge_index],
                pos_y[u] + sign * dy[edge_index],
                pos_z[u] + sign * dz[edge_index],
            )
            if unseen[v]:
                unseen[v] = 0
                pos_x[v], pos_y[v], pos_z[v] = predicted
                queue[count] = v
                count += 1
            elif (pos_x[v], pos_y[v], pos_z[v]) != predicted:
                raise ValueError("remaining ties admit no exact position")
    if count != n:
        raise ValueError("remaining ties disconnect a fragment from the baseline")
    return pos_x, pos_y, pos_z


# ---------------------------------------------------------------------------
# Fragment decisions and evidence
#


def _sorted_candidate_ranks(analysis, plan):
    """Candidate ranks ordered by their declared record id (stable evidence)."""
    record = plan["edge_record"]
    return sorted(
        range(len(analysis["cand_edge"])),
        key=lambda rank: record[analysis["cand_edge"][rank]],
    )


def _reason_for(decision, status=RESOLVED):
    if decision == RELEASED and status == CONSISTENT:
        return "all declared ties are simultaneously satisfied"
    if decision == RELEASED:
        return "unique origin under every feasible single-tie discard"
    if decision == WITHHELD:
        return (
            "origin ambiguous: feasible single-tie discards give distinct "
            "positions; both are recomputable from the plan"
        )
    return (
        "batch unexplained: no single tentative tie can be discarded to leave "
        "one exact connected position table"
    )


def decide_fragments(plan, analysis):
    """Compute one compact decision per fragment, including evidence.

    The result stores parallel typed arrays rather than one dict per fragment:
    a decision code per node, one released-origin triple and, for withheld
    fragments, two candidate ranks with their distinct, recomputable origins.
    """
    from array import array as _typed_array

    fragments = plan["fragments"]
    n = len(fragments)
    tree_x, tree_y, tree_z = analysis["positions"]
    status = analysis["status"]
    ranks = _sorted_candidate_ranks(analysis, plan)

    origin_x = _typed_array("q", [0]) * n
    origin_y = _typed_array("q", [0]) * n
    origin_z = _typed_array("q", [0]) * n
    decision_codes = bytearray(n)
    # Withholding evidence; only the withheld entries are meaningful.
    witness_rank_a = _typed_array("i", [-1]) * n
    witness_rank_b = _typed_array("i", [-1]) * n
    witness_ax = _typed_array("q", [0]) * n
    witness_ay = _typed_array("q", [0]) * n
    witness_az = _typed_array("q", [0]) * n
    witness_bx = _typed_array("q", [0]) * n
    witness_by = _typed_array("q", [0]) * n
    witness_bz = _typed_array("q", [0]) * n

    if status == CONSISTENT:
        for node in range(n):
            decision_codes[node] = 1
            origin_x[node], origin_y[node], origin_z[node] = (
                tree_x[node], tree_y[node], tree_z[node]
            )
    elif status == UNEXPLAINED:
        # Every fragment stays unreleased; positions must not be published.
        pass
    else:
        parent_node = analysis["parent_node"]
        root = plan["baseline_index"]
        # First-child/next-sibling representation avoids one list per fragment.
        first_child = _typed_array("i", [-1]) * n
        next_sibling = _typed_array("i", [-1]) * n
        for node in range(n):
            parent = parent_node[node]
            if parent >= 0:
                next_sibling[node] = first_child[parent]
                first_child[parent] = node

        cand_root = analysis["cand_root"]
        cand_sx = analysis["cand_sx"]
        cand_sy = analysis["cand_sy"]
        cand_sz = analysis["cand_sz"]
        # Everything below indexes the id-sorted candidate order, so map each
        # analysis rank onto its sorted position once.
        rank_to_pos = {rank: pos for pos, rank in enumerate(ranks)}
        # Subtree root node -> sorted position of the candidate covering it.
        tree_candidate_pos = {}
        for pos, rank in enumerate(ranks):
            if cand_root[rank] >= 0:
                tree_candidate_pos[cand_root[rank]] = pos

        covering = bytearray(len(ranks))
        inactive_pointer = 0
        # Shift vector -> smallest covering candidate record id on the path.
        active = {}
        unique_x = _typed_array("q", [0]) * n
        unique_y = _typed_array("q", [0]) * n
        unique_z = _typed_array("q", [0]) * n
        record_to_pos = {
            plan["edge_record"][analysis["cand_edge"][rank]]: pos
            for pos, rank in enumerate(ranks)
        }

        def first_inactive():
            nonlocal inactive_pointer
            while (
                inactive_pointer < len(ranks) and covering[inactive_pointer]
            ):
                inactive_pointer += 1
            return inactive_pointer

        def evaluate(node):
            classes = dict(active)
            zero_pos = first_inactive()
            if zero_pos < len(ranks):
                zero_id = plan["edge_record"][
                    analysis["cand_edge"][ranks[zero_pos]]
                ]
                if (0, 0, 0) not in classes or zero_id < classes[(0, 0, 0)]:
                    classes[(0, 0, 0)] = zero_id
            if len(classes) <= 1:
                decision_codes[node] = 1
                shift = next(iter(classes), (0, 0, 0))
                unique_x[node], unique_y[node], unique_z[node] = shift
                return
            decision_codes[node] = 2
            ordered = sorted(classes.items(), key=lambda item: item[1])
            first_id, first_shift = ordered[0][1], ordered[0][0]
            second_id, second_shift = ordered[1][1], ordered[1][0]
            base = (tree_x[node], tree_y[node], tree_z[node])
            first_origin = _add(base, first_shift)
            second_origin = _add(base, second_shift)
            if first_origin == second_origin:
                raise RuntimeError(
                    "internal error: ambiguous evidence positions coincide"
                )
            witness_rank_a[node] = ranks[record_to_pos[first_id]]
            witness_rank_b[node] = ranks[record_to_pos[second_id]]
            witness_ax[node], witness_ay[node], witness_az[node] = first_origin
            witness_bx[node], witness_by[node], witness_bz[node] = second_origin

        # Iterative DFS; exit frames carry their own entry mutation.
        stack = [(root, True, None, None)]
        while stack:
            node, entering, frame_pos, change = stack.pop()
            if not entering:
                if frame_pos is not None:
                    covering[frame_pos] = 0
                    if frame_pos < inactive_pointer:
                        inactive_pointer = frame_pos
                if change is not None:
                    if change[0] == "add":
                        del active[change[1]]
                    else:
                        active[change[1]] = change[2]
                continue
            pos = tree_candidate_pos.get(node)
            change = None
            if pos is not None:
                rank = ranks[pos]
                covering[pos] = 1
                shift = (cand_sx[rank], cand_sy[rank], cand_sz[rank])
                record_id = plan["edge_record"][analysis["cand_edge"][rank]]
                if shift not in active:
                    active[shift] = record_id
                    change = ("add", shift)
                elif record_id < active[shift]:
                    change = ("replace", shift, active[shift])
                    active[shift] = record_id
            evaluate(node)
            stack.append((node, False, pos, change))
            child = first_child[node]
            while child >= 0:
                stack.append((child, True, None, None))
                child = next_sibling[child]

        for node in range(n):
            if decision_codes[node] == 1:
                origin_x[node] = tree_x[node] + unique_x[node]
                origin_y[node] = tree_y[node] + unique_y[node]
                origin_z[node] = tree_z[node] + unique_z[node]

    counts = {
        RELEASED: sum(1 for code in decision_codes if code == 1),
        WITHHELD: sum(1 for code in decision_codes if code == 2),
        NOT_RELEASED: sum(1 for code in decision_codes if code == 0),
    }
    return {
        "status": status,
        "ranks": ranks,
        "decision_codes": decision_codes,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "origin_z": origin_z,
        "witness_rank_a": witness_rank_a,
        "witness_rank_b": witness_rank_b,
        "witness_ax": witness_ax,
        "witness_ay": witness_ay,
        "witness_az": witness_az,
        "witness_bx": witness_bx,
        "witness_by": witness_by,
        "witness_bz": witness_bz,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Merged points and streaming package
#


def _released_origins(plan, result):
    """Fragment id -> exact origin in metres for released fragments only."""
    index_by_id = plan["index_by_id"]
    codes = result["decision_codes"]
    origins = {}
    for fragment_id, node in index_by_id.items():
        if codes[node] == 1:
            origins[fragment_id] = (
                Fraction(result["origin_x"][node], MICRONS_PER_METRE),
                Fraction(result["origin_y"][node], MICRONS_PER_METRE),
                Fraction(result["origin_z"][node], MICRONS_PER_METRE),
            )
    return origins


def iter_merged_points(plan, views, result):
    """Yield merged point rows in identity order, one view at a time."""
    origins = _released_origins(plan, result)
    for fragment_id in sorted(views):
        if fragment_id not in origins:
            continue
        origin_m = origins[fragment_id]
        rows = []
        for row in views[fragment_id]:
            xyz = [Fraction(cell) for cell in row["xyz"]]
            rows.append({
                "fragment_id": fragment_id,
                "sequence": row["sequence"],
                "track_id": row["track_id"],
                "xyz": [
                    rational_text(xyz[axis] + origin_m[axis]) for axis in range(3)
                ],
            })
        rows.sort(key=lambda row: (row["sequence"], row["track_id"]))
        for row in rows:
            yield row


def _canonical_dump(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=True,
    ).encode("utf-8")


def _origin_text(origin_um):
    return [
        rational_text(Fraction(component, MICRONS_PER_METRE))
        for component in origin_um
    ]


def _fragment_chunk(plan, analysis, result, node):
    """Canonical JSON bytes for one fragment decision, sorted object keys."""
    fragment = plan["fragments"][node]
    code = result["decision_codes"][node]
    if code == 1:
        decision = RELEASED
    elif code == 2:
        decision = WITHHELD
    else:
        decision = NOT_RELEASED
    origin_um = None
    origin_m = None
    evidence = None
    if code == 1:
        origin_um = [
            result["origin_x"][node],
            result["origin_y"][node],
            result["origin_z"][node],
        ]
        origin_m = _origin_text(origin_um)
    elif code == 2:
        edge_record = plan["edge_record"]
        cand_edge = analysis["cand_edge"]
        first_rank = result["witness_rank_a"][node]
        second_rank = result["witness_rank_b"][node]
        first_origin = [
            result["witness_ax"][node],
            result["witness_ay"][node],
            result["witness_az"][node],
        ]
        second_origin = [
            result["witness_bx"][node],
            result["witness_by"][node],
            result["witness_bz"][node],
        ]
        evidence = [
            {
                "record_id": edge_record[cand_edge[first_rank]],
                "origin_m": _origin_text(first_origin),
                "origin_um": first_origin,
            },
            {
                "record_id": edge_record[cand_edge[second_rank]],
                "origin_m": _origin_text(second_origin),
                "origin_um": second_origin,
            },
        ]
    document = {
        "decision": decision,
        "endpoint": fragment["endpoint"],
        "evidence": evidence,
        "fragment_id": fragment["fragment_id"],
        "has_table": fragment["endpoint"] is not None,
        "origin_m": origin_m,
        "origin_um": origin_um,
        "reason": _reason_for(decision, analysis["status"]),
    }
    return _canonical_dump(document)


def _endpoint_documents(plan):
    documents = []
    for fragment_id in sorted(plan["endpoints"]):
        endpoint = plan["endpoints"][fragment_id]
        documents.append({
            "dataset_id": endpoint["dataset_id"],
            "fingerprint": endpoint["fingerprint"],
            "fragment_id": fragment_id,
            "version": endpoint["version"],
        })
    return documents


def _endpoint_chunks(plan):
    return [_canonical_dump(doc) for doc in _endpoint_documents(plan)]


def _feasible_records(plan, analysis, result):
    return [
        plan["edge_record"][analysis["cand_edge"][rank]]
        for rank in result["ranks"]
    ]


def write_package(stream, plan, analysis, result, views):
    """Stream one deterministic package; returns the printed summary.

    Merged point rows are produced twice (once for the digest, once for the
    bytes) so neither the canonical row list nor the whole payload has to be
    held in memory at once.
    """
    write = stream.write
    if analysis["status"] == UNEXPLAINED and any(
        code != 0 for code in result["decision_codes"]
    ):
        raise RuntimeError("unexplained batch must deliver no points")

    def point_rows():
        if analysis["status"] == UNEXPLAINED:
            return
        for row in iter_merged_points(plan, views, result):
            yield row

    endpoint_documents = _endpoint_documents(plan)
    endpoint_chunks = [_canonical_dump(doc) for doc in endpoint_documents]
    feasible = _feasible_records(plan, analysis, result)
    fragment_count = len(plan["fragments"])
    # Emission order follows fragment ids, never plan array order, so the
    # bytes are independent of declaration reordering.
    ordered_nodes = sorted(
        range(fragment_count),
        key=lambda node: plan["fragments"][node]["fragment_id"],
    )

    point_count = 0
    digest = hashlib.sha256()
    digest.update(PACKAGE_FORMAT.encode("ascii"))
    digest.update(
        _canonical_dump({
            "plan_id": plan["plan_id"],
            "baseline": plan["baseline"],
            "status": analysis["status"],
            "feasible_discards": feasible,
            "endpoints": endpoint_documents,
        })
    )
    for node in ordered_nodes:
        digest.update(_fragment_chunk(plan, analysis, result, node))
    for row in point_rows():
        digest.update(_canonical_dump(row))
        point_count += 1
    package_digest = PACKAGE_FORMAT + ":" + digest.hexdigest()

    header = {
        "schema_version": SCHEMA_VERSION,
        "kind": PACKAGE_KIND,
        "package_format": PACKAGE_FORMAT,
        "plan_id": plan["plan_id"],
        "baseline": plan["baseline"],
        "status": analysis["status"],
        "feasible_discards": feasible,
        "package_digest": package_digest,
        "fragment_count": fragment_count,
        "point_count": point_count,
        "released_count": result["counts"][RELEASED],
        "withheld_count": result["counts"][WITHHELD],
        "not_released_count": result["counts"][NOT_RELEASED],
    }
    write(b"{")
    first = True
    for key in sorted(header):
        if not first:
            write(b",")
        first = False
        write(_canonical_dump(key))
        write(b":")
        write(_canonical_dump(header[key]))
    write(b',"endpoints":[')
    for index, chunk in enumerate(endpoint_chunks):
        if index:
            write(b",")
        write(chunk)
    write(b'],"fragments":[')
    for index, node in enumerate(ordered_nodes):
        if index:
            write(b",")
        write(_fragment_chunk(plan, analysis, result, node))
    write(b'],"points":[')
    first_point = True
    for row in point_rows():
        if not first_point:
            write(b",")
        first_point = False
        write(_canonical_dump(row))
    write(b"]}\n")
    return {
        "plan_id": plan["plan_id"],
        "status": analysis["status"],
        "baseline": plan["baseline"],
        "feasible_discards": feasible,
        "fragments": fragment_count,
        "released": result["counts"][RELEASED],
        "withheld": result["counts"][WITHHELD],
        "not_released": result["counts"][NOT_RELEASED],
        "points": point_count,
    }


def run(plan_path, input_map, output_path):
    output = Path(output_path)
    input_paths = {Path(plan_path).resolve()}
    for view_path in input_map.values():
        input_paths.add(Path(view_path).resolve())
    if output.resolve() in input_paths:
        raise ValueError("output path must differ from the plan and view inputs")
    if output.exists() or output.is_symlink():
        raise ValueError(f"merge package already exists: {output_path}")

    plan = load_plan(plan_path)
    views = resolve_inputs(plan, input_map)
    analysis = analyze(plan)
    result = decide_fragments(plan, analysis)

    # One full independent re-solve per run guards the shift arithmetic used
    # for the bulk of the candidates; it costs a single O(V+E) BFS.
    if result["ranks"]:
        first_edge = analysis["cand_edge"][result["ranks"][0]]
        solve_without(plan, first_edge)

    holder = {}

    def write(stream):
        holder["summary"] = write_package(stream, plan, analysis, result, views)

    # Every failure inside the writer (including an unexplained-batch
    # invariant) happens before the hard link, so no partial target appears.
    checkpoint(MERGE_BEFORE_WRITE)
    io.publish_new_stream(output, write)
    summary = holder["summary"]
    summary["output"] = str(output_path)
    return summary
