"""Deterministic bipartite matching primitives for the acceptance report.

Graphs are bipartite over ``range(n_a)`` and ``range(n_b)`` with edges given
as sorted adjacency lists for the left side.  Every routine is a pure
function of the edge set: results never depend on dictionary iteration or
input array order, so reports built on them are reproducible.
"""

import heapq
import sys
from bisect import bisect_left
from collections import deque


def _recursion_budget(size):
    # Pure-Python recursion does not grow the C stack on CPython 3.11+.
    if sys.getrecursionlimit() < size:
        sys.setrecursionlimit(size)


def hopcroft_karp(adjacency, n_b):
    """Maximum matching; returns ``(match_a, match_b)`` with -1 meaning free."""
    n_a = len(adjacency)
    match_a = [-1] * n_a
    match_b = [-1] * n_b
    _recursion_budget(4 * (n_a + n_b) + 1000)

    def layered():
        dist = [-1] * n_a
        queue = deque(a for a in range(n_a) if match_a[a] == -1)
        for a in queue:
            dist[a] = 0
        found = False
        while queue:
            a = queue.popleft()
            for b in adjacency[a]:
                other = match_b[b]
                if other == -1:
                    found = True
                elif dist[other] == -1:
                    dist[other] = dist[a] + 1
                    queue.append(other)
        return dist if found else None

    def extend(a, dist, ptr):
        while ptr[a] < len(adjacency[a]):
            b = adjacency[a][ptr[a]]
            ptr[a] += 1
            other = match_b[b]
            if other == -1 or (
                dist[other] == dist[a] + 1 and extend(other, dist, ptr)
            ):
                match_a[a] = b
                match_b[b] = a
                return True
        return False

    while True:
        dist = layered()
        if dist is None:
            return match_a, match_b
        ptr = [0] * n_a
        for a in range(n_a):
            if match_a[a] == -1:
                extend(a, dist, ptr)


def alternating_reach(adjacency, match_a, match_b):
    """Vertices reachable from free left vertices by alternating paths.

    Unmatched edges are followed left to right, matched edges right to left.
    Returns ``(seen_a, seen_b)`` boolean lists.  For a maximum matching the
    reached left set is a Hall-deficient witness: its neighbour set is the
    reached right set, which is strictly smaller.  The reached sets do not
    depend on which maximum matching was found (Dulmage-Mendelsohn).
    """
    n_a = len(adjacency)
    n_b = len(match_b)
    seen_a = [False] * n_a
    seen_b = [False] * n_b
    queue = deque()
    for a in range(n_a):
        if match_a[a] == -1:
            seen_a[a] = True
            queue.append(a)
    while queue:
        a = queue.popleft()
        for b in adjacency[a]:
            if match_a[a] != b and not seen_b[b]:
                seen_b[b] = True
                partner = match_b[b]
                if partner != -1 and not seen_a[partner]:
                    seen_a[partner] = True
                    queue.append(partner)
    return seen_a, seen_b


def _strong_components(arcs):
    """Iterative Tarjan; returns a component id per node."""
    size = len(arcs)
    index_of = [-1] * size
    low = [0] * size
    on_stack = [False] * size
    stack = []
    component = [-1] * size
    counter = 0
    count = 0
    for start in range(size):
        if index_of[start] != -1:
            continue
        work = [(start, 0)]
        while work:
            node, child = work[-1]
            if child == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack[node] = True
            descended = False
            for edge in range(child, len(arcs[node])):
                other = arcs[node][edge]
                if index_of[other] == -1:
                    work[-1] = (node, edge + 1)
                    work.append((other, 0))
                    descended = True
                    break
                if on_stack[other] and index_of[other] < low[node]:
                    low[node] = index_of[other]
            if descended:
                continue
            work.pop()
            if work and low[node] < low[work[-1][0]]:
                low[work[-1][0]] = low[node]
            if low[node] == index_of[node]:
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component[member] = count
                    if member == node:
                        break
                count += 1
    return component


def allowed_partners(adjacency, match_a, match_b):
    """Right neighbours each left vertex has in at least one perfect matching.

    ``match_a``/``match_b`` must describe a perfect matching.  A matched edge
    is always allowed; an unmatched edge is allowed exactly when its endpoints
    share a strongly connected component of the directed graph that orients
    unmatched edges left to right and matched edges right to left.  The result
    is a property of the graph, not of the particular matching used.
    """
    n_a = len(adjacency)
    n_b = len(match_b)
    arcs = [[] for _ in range(n_a + n_b)]
    for a in range(n_a):
        for b in adjacency[a]:
            if match_a[a] == b:
                arcs[n_a + b].append(a)
            else:
                arcs[a].append(n_a + b)
    component = _strong_components(arcs)
    return [
        [b for b in adjacency[a] if match_a[a] == b or component[a] == component[n_a + b]]
        for a in range(n_a)
    ]


def min_cost_matching(adjacency, costs, n_b):
    """Minimum-cost perfect matching with 0/1 edge costs.

    ``costs`` parallels ``adjacency``.  Returns ``(cost, match_a)`` or
    ``None`` when no perfect matching exists.  The flow is initialised with a
    maximum matching of the zero-cost edges (already optimal for its value),
    then augmented by shortest-path phases: one Dijkstra over reduced costs
    plus every tight augmenting path per phase.
    """
    n_a = len(adjacency)
    source = n_a + n_b
    sink = source + 1
    size = sink + 1
    graph = [[] for _ in range(size)]

    def add_arc(u, v, capacity, cost):
        graph[u].append([v, capacity, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, len(graph[u]) - 1])

    zero_adjacency = [[] for _ in range(n_a)]
    arc_index = []
    for a in range(n_a):
        row = []
        for k, b in enumerate(adjacency[a]):
            row.append(len(graph[a]))
            add_arc(a, n_a + b, 1, costs[a][k])
            if costs[a][k] == 0:
                zero_adjacency[a].append(b)
        arc_index.append(row)
    source_arc = []
    for a in range(n_a):
        source_arc.append(len(graph[source]))
        add_arc(source, a, 1, 0)
    sink_arc = []
    for b in range(n_b):
        sink_arc.append(len(graph[n_a + b]))
        add_arc(n_a + b, sink, 1, 0)

    def saturate(u, i):
        arc = graph[u][i]
        arc[1] -= 1
        graph[arc[0]][arc[3]][1] += 1

    match_a, _ = hopcroft_karp(zero_adjacency, n_b)
    flow = 0
    for a in range(n_a):
        b = match_a[a]
        if b == -1:
            continue
        flow += 1
        saturate(source, source_arc[a])
        k = bisect_left(adjacency[a], b)
        saturate(a, arc_index[a][k])
        saturate(n_a + b, sink_arc[b])

    potential = [0] * size
    _recursion_budget(4 * size + 1000)
    while flow < n_a:
        dist = [None] * size
        dist[source] = 0
        heap = [(0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if d != dist[u]:
                continue
            for arc in graph[u]:
                if arc[1] > 0:
                    v = arc[0]
                    w = d + arc[2] + potential[u] - potential[v]
                    if dist[v] is None or w < dist[v]:
                        dist[v] = w
                        heapq.heappush(heap, (w, v))
        if dist[sink] is None:
            return None
        ceiling = dist[sink]
        for u in range(size):
            reached = dist[u]
            potential[u] += ceiling if reached is None or reached > ceiling else reached
        ptr = [0] * size
        blocked = [False] * size

        def drain(u):
            if u == sink:
                return True
            if blocked[u]:
                return False
            while ptr[u] < len(graph[u]):
                arc = graph[u][ptr[u]]
                ptr[u] += 1
                v = arc[0]
                if (
                    arc[1] > 0
                    and not blocked[v]
                    and arc[2] == potential[v] - potential[u]
                    and drain(v)
                ):
                    arc[1] -= 1
                    graph[v][arc[3]][1] += 1
                    return True
            blocked[u] = True
            return False

        while flow < n_a and drain(source):
            flow += 1

    match = [-1] * n_a
    total = 0
    for a in range(n_a):
        for k, b in enumerate(adjacency[a]):
            if graph[a][arc_index[a][k]][1] == 0:
                match[a] = b
                total += costs[a][k]
                break
    return total, match
