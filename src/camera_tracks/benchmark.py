"""Offline training-material and blind-benchmark release registry.

A benchmark project freezes 1..8 delivered ``view`` point tables together
with the reviewed associations between their acquisition sources.  Training
releases and blind test booklets are then registered against that frozen
snapshot:

* a training release opens the listed points (and, through the association
  graph, their whole relation groups) to the training side;
* a booklet may only be issued while none of its points is exposed;
* appending relations or publishing more training material can expose a
  point later.  Every still-usable booklet with an exposed point is then
  withdrawn as part of the same atomic operation, its first withdrawal
  evidence is frozen, and all of its old scores leave the comparable table;
* operation ids are globally idempotent: a successfully committed request is
  answered with its original receipt even after later withdrawals.

Everything is exact: point coordinates are the stored canonical rational
strings and elevation comparisons use ``Fraction``.  The project state is one
local JSON document; each successful operation rewrites it with one atomic
replacement, so a process interruption leaves either the old or the new
revision, never a half-applied one.
"""

import hashlib
from fractions import Fraction
from pathlib import Path

from . import io
from .checks import (
    array,
    exact_array,
    fields,
    ident,
    integer,
    number,
    rational,
)
from .faults import (
    BENCH_AFTER_COMMIT,
    BENCH_BEFORE_COMMIT,
    BENCH_EXPORT_BEFORE_WRITE,
    checkpoint,
)
from .state import (
    MAX_SEQUENCE,
    MAX_VERSIONS,
    SCHEMA_VERSION,
    _digest,
    canonical_json,
    fingerprint_points,
    point_key,
    rational_text,
)

PROJECT_KIND = "camera-tracks-benchmark-project"
CONFIG_KIND = "camera-tracks-benchmark-config"
TRAINING_KIND = "camera-tracks-training-package"
BOOKLET_KIND = "camera-tracks-benchmark-booklet"
TRAINING_FORMAT = "camera-tracks-training-v1"
BOOKLET_FORMAT = "camera-tracks-booklet-v1"
VIEW_KIND = "camera-tracks-view"

KIND_RELEASE = "release"
KIND_ISSUE = "issue"
KIND_RELATE = "relate"
KIND_SCORE = "score"

MAX_VIEWS = 8
MAX_PROJECT_POINTS = 80_000
MAX_PROJECT_RELATIONS = 120_000
MAX_BOOKLET_POINTS = 4096
MAX_MATERIALS = 1_000_000
# A relation batch can carry up to 120,000 full point references and a
# training list up to 80,000, so requests and configs get a larger cap than
# the default small-document reader.
MAX_REQUEST_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Point identities


def _point_ref(value, where):
    """Parse one explicit ``{dataset_id, sequence, track_id}`` reference."""
    fields(value, ("dataset_id", "sequence", "track_id"), where=where)
    return (
        ident(value["dataset_id"], f"{where} dataset_id"),
        integer(value["sequence"], 0, MAX_SEQUENCE, f"{where} sequence"),
        ident(value["track_id"], f"{where} track_id"),
    )


def _stored_ref(value, where):
    """Parse a compact stored ``[dataset_id, sequence, track_id]`` triple."""
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{where} must be a [dataset_id, sequence, track_id] array")
    return (
        ident(value[0], f"{where} dataset_id"),
        integer(value[1], 0, MAX_SEQUENCE, f"{where} sequence"),
        ident(value[2], f"{where} track_id"),
    )


def _stored_pair(value, where):
    """Parse one stored unordered relation pair, kept in canonical order."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{where} must hold exactly two point references")
    first = _stored_ref(value[0], f"{where} first")
    second = _stored_ref(value[1], f"{where} second")
    if first == second:
        raise ValueError(f"{where} must join two different points")
    ordered = tuple(sorted((first, second)))
    if list(ordered[0]) != value[0] or list(ordered[1]) != value[1]:
        raise ValueError(f"{where} endpoints are not in canonical order")
    return ordered


def _ref_json(ref):
    return {
        "dataset_id": ref[0],
        "sequence": ref[1],
        "track_id": ref[2],
    }


def _ref_sort_key(ref):
    return (ref[0], ref[1], ref[2])


def _pair_json(pair):
    return [_ref_json(pair[0]), _ref_json(pair[1])]


# ---------------------------------------------------------------------------
# Numbers and canonical requests


def _limit_text(value, where="elevation_limit"):
    """A non-negative bounded decimal string, rendered by exact value."""
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a bounded decimal string")
    parsed = number(value, where)
    if parsed < 0:
        raise ValueError(f"{where} must be non-negative")
    return rational_text(parsed)


def _stored_limit(value, where):
    """A stored limit is a canonical non-negative exact-rational text."""
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a rational string")
    rational(value, where)
    if Fraction(value) < 0:
        raise ValueError(f"{where} must be non-negative")
    return value


def _prediction_value(value, where):
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a bounded decimal string")
    return number(value, where)


def _digest_of(canonical_request):
    return hashlib.sha256(
        _canonical_request_bytes(canonical_request)
    ).hexdigest()


def _canonical_request_bytes(value):
    # Compact sorted-key JSON, independent of object key order and whitespace.
    return canonical_json(value)


def _parse_release_request(document):
    fields(document, ("op_id", "release_id", "points"), where="release request")
    op_id = ident(document["op_id"], "op_id")
    release_id = ident(document["release_id"], "release_id")
    entries = array(document["points"], 1, MAX_PROJECT_POINTS, "release points")
    refs = [
        _point_ref(entry, f"release point {index}")
        for index, entry in enumerate(entries)
    ]
    if len(set(refs)) != len(refs):
        raise ValueError("release points must be distinct")
    refs.sort(key=_ref_sort_key)
    canonical = {
        "kind": KIND_RELEASE,
        "release_id": release_id,
        "points": [_ref_json(ref) for ref in refs],
    }
    return op_id, canonical, {"release_id": release_id, "points": refs}


def _parse_issue_request(document):
    fields(
        document,
        ("op_id", "booklet_id", "elevation_limit", "points"),
        where="issue request",
    )
    op_id = ident(document["op_id"], "op_id")
    booklet_id = ident(document["booklet_id"], "booklet_id")
    limit = _limit_text(document["elevation_limit"])
    entries = array(
        document["points"], 1, MAX_BOOKLET_POINTS, "booklet points"
    )
    refs = [
        _point_ref(entry, f"booklet point {index}")
        for index, entry in enumerate(entries)
    ]
    if len(set(refs)) != len(refs):
        raise ValueError("booklet points must be distinct")
    refs.sort(key=_ref_sort_key)
    canonical = {
        "kind": KIND_ISSUE,
        "booklet_id": booklet_id,
        "elevation_limit": limit,
        "points": [_ref_json(ref) for ref in refs],
    }
    return op_id, canonical, {
        "booklet_id": booklet_id,
        "elevation_limit": limit,
        "points": refs,
    }


def _parse_relate_request(document):
    fields(document, ("op_id", "relations"), where="relation request")
    op_id = ident(document["op_id"], "op_id")
    entries = array(
        document["relations"], 1, MAX_PROJECT_RELATIONS, "relations"
    )
    pairs = []
    for index, entry in enumerate(entries):
        fields(entry, ("points",), where=f"relation {index}")
        raw = exact_array(entry["points"], 2, f"relation {index} points")
        first = _point_ref(raw[0], f"relation {index} first")
        second = _point_ref(raw[1], f"relation {index} second")
        if first == second:
            raise ValueError(f"relation {index} must join two different points")
        pairs.append(tuple(sorted((first, second))))
    if len(set(pairs)) != len(pairs):
        raise ValueError("duplicate relation endpoint pair in one batch")
    pairs.sort(key=lambda pair: (_ref_sort_key(pair[0]), _ref_sort_key(pair[1])))
    canonical = {
        "kind": KIND_RELATE,
        "relations": [_pair_json(pair) for pair in pairs],
    }
    return op_id, canonical, {"pairs": pairs}


def _parse_score_request(document):
    fields(
        document,
        ("op_id", "booklet_id", "booklet_sha256", "prediction_id", "predictions"),
        where="score request",
    )
    op_id = ident(document["op_id"], "op_id")
    booklet_id = ident(document["booklet_id"], "booklet_id")
    booklet_sha = _digest(document["booklet_sha256"], "booklet_sha256")
    prediction_id = ident(document["prediction_id"], "prediction_id")
    entries = array(
        document["predictions"], 1, MAX_BOOKLET_POINTS, "predictions"
    )
    predictions = []
    for index, entry in enumerate(entries):
        where = f"prediction {index}"
        fields(entry, ("dataset_id", "sequence", "track_id", "z"), where=where)
        ref = (
            ident(entry["dataset_id"], f"{where} dataset_id"),
            integer(entry["sequence"], 0, MAX_SEQUENCE, f"{where} sequence"),
            ident(entry["track_id"], f"{where} track_id"),
        )
        value = _prediction_value(entry["z"], f"{where} z")
        predictions.append((ref, value))
    refs = [ref for ref, _value in predictions]
    if len(set(refs)) != len(refs):
        raise ValueError("duplicate prediction point")
    predictions.sort(key=lambda item: _ref_sort_key(item[0]))
    canonical = {
        "kind": KIND_SCORE,
        "booklet_id": booklet_id,
        "booklet_sha256": booklet_sha,
        "prediction_id": prediction_id,
        "predictions": [
            {"point": _ref_json(ref), "z": rational_text(value)}
            for ref, value in predictions
        ],
    }
    return op_id, canonical, {
        "booklet_id": booklet_id,
        "booklet_sha256": booklet_sha,
        "prediction_id": prediction_id,
        "predictions": predictions,
    }


# ---------------------------------------------------------------------------
# Project document: point tables, relations and registered material


def _view_point_table(dataset_id, document, declared):
    """Validate one bound view and return its frozen point table.

    The point-table fingerprint is always recomputed from the point rows and
    must equal the declared digest; ``dataset_id``, ``version`` and
    ``fingerprint`` must all match the configuration.  An equal fingerprint
    cannot stand in for another version.
    """
    fields(
        document,
        ("schema_version", "kind", "dataset_id", "version", "fingerprint", "points"),
        where=f"view {dataset_id}",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"view {dataset_id} has unsupported schema_version")
    if document["kind"] != VIEW_KIND:
        raise ValueError(f"view {dataset_id} is not a camera-tracks view")
    actual_dataset = ident(document["dataset_id"], f"view {dataset_id} dataset_id")
    actual_version = integer(
        document["version"], 0, MAX_VERSIONS, f"view {dataset_id} version"
    )
    _digest(document["fingerprint"], f"view {dataset_id} fingerprint")
    batches = {}
    table = {}
    for position, entry in enumerate(
        array(document["points"], 0, MAX_PROJECT_POINTS, f"view {dataset_id} points")
    ):
        where = f"view {dataset_id} point {position}"
        fields(entry, ("sequence", "track_id", "xyz"), where=where)
        sequence = integer(entry["sequence"], 0, MAX_SEQUENCE, f"{where} sequence")
        track_id = ident(entry["track_id"], f"{where} track_id")
        exact_array(entry["xyz"], 3, f"{where} xyz")
        for cell in entry["xyz"]:
            rational(cell, f"{where} xyz")
        key = point_key(sequence, track_id)
        if key in table:
            raise ValueError(f"view {dataset_id} repeats point identity: {key}")
        table[key] = list(entry["xyz"])
        batches.setdefault(str(sequence), {
            "sequence": sequence,
            "points": {},
        })["points"][track_id] = list(entry["xyz"])
    if fingerprint_points(table) != document["fingerprint"]:
        raise ValueError(f"view {dataset_id} fingerprint does not match its point table")
    for field in ("dataset_id", "version", "fingerprint"):
        if document[field] != declared[field]:
            raise ValueError(
                f"view {dataset_id} {field} {document[field]!r} does not match "
                f"the configured {declared[field]!r}"
            )
    ordered = {str(sequence): batches[str(sequence)]
               for sequence in sorted(int(key) for key in batches)}
    return {
        "version": actual_version,
        "fingerprint": document["fingerprint"],
        "batches": ordered,
    }


def _point_exists(project, ref):
    dataset = project["datasets"].get(ref[0])
    if dataset is None:
        return False
    batch = dataset["batches"].get(str(ref[1]))
    if batch is None:
        return False
    return ref[2] in batch["points"]


def _point_xyz(project, ref):
    return project["datasets"][ref[0]]["batches"][str(ref[1])]["points"][ref[2]]


def _total_points(project):
    return sum(
        len(batch["points"])
        for dataset in project["datasets"].values()
        for batch in dataset["batches"].values()
    )


def _endpoint(project, ref):
    dataset = project["datasets"][ref[0]]
    return {
        "dataset_id": ref[0],
        "version": dataset["version"],
        "fingerprint": dataset["fingerprint"],
    }


# ---------------------------------------------------------------------------
# Relation groups and exposure


def _node_index(project):
    """Every acquisition batch is one node; points of a batch share a node."""
    index = {}
    for dataset_id in sorted(project["datasets"]):
        for sequence_text in sorted(
            project["datasets"][dataset_id]["batches"], key=int
        ):
            index[(dataset_id, int(sequence_text))] = len(index)
    return index


class _UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, node):
        parent = self.parent
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(self, first, second):
        first = self.find(first)
        second = self.find(second)
        if first == second:
            return
        if self.rank[first] < self.rank[second]:
            first, second = second, first
        self.parent[second] = first
        if self.rank[first] == self.rank[second]:
            self.rank[first] += 1


def _exposure(project):
    """Compute exposed components, training material and withdrawal evidence.

    A component is exposed when any published training point belongs to it;
    because all points of one acquisition batch share a node, publishing one
    point of a batch exposes the rest of that batch as well.
    """
    nodes = _node_index(project)
    union = _UnionFind(len(nodes))
    for first, second in project["relations"]:
        union.union(nodes[(first[0], first[1])], nodes[(second[0], second[1])])
    exposed = set()
    published = {}
    for release_id in sorted(project["releases"]):
        release = project["releases"][release_id]
        for ref in release["points"]:
            node = nodes[(ref[0], ref[1])]
            root = union.find(node)
            exposed.add(root)
            published.setdefault(root, []).append((tuple(ref), release_id))
    for entries in published.values():
        entries.sort()
    return {
        "nodes": nodes,
        "find": union.find,
        "exposed": exposed,
        "published": published,
    }


def _evidence_for(exposure, root, test_point):
    """Earliest published training point in the group, preferring a distinct
    one so the evidence never degenerates to the test point itself."""
    entries = exposure["published"][root]
    for ref, release_id in entries:
        if ref != test_point:
            return ref, release_id
    ref, release_id = entries[0]
    return ref, release_id


def _point_exposed(exposure, ref):
    node = exposure["nodes"].get((ref[0], ref[1]))
    if node is None:
        return False
    return exposure["find"](node) in exposure["exposed"]


def _withdraw_booklets(project, causing_op_id, revision):
    """Withdraw every active booklet with an exposed point, in one revision.

    The first withdrawal evidence is frozen here: one test point of the
    booklet, one already published training point in the same connected
    group, and the earliest release id that published that training point.
    Later relation edges can only add connectivity, so the stored evidence
    never becomes false and is never rewritten.
    """
    exposure = _exposure(project)
    withdrawn = []
    for booklet_id in sorted(project["booklets"]):
        booklet = project["booklets"][booklet_id]
        if booklet["withdrawal"] is not None:
            continue
        test_point = next(
            (ref for ref in booklet["points"] if _point_exposed(exposure, ref)),
            None,
        )
        if test_point is None:
            continue
        node = exposure["nodes"][(test_point[0], test_point[1])]
        root = exposure["find"](node)
        training_point, release_id = _evidence_for(exposure, root, tuple(test_point))
        booklet["withdrawal"] = {
            "revision": revision,
            "op_id": causing_op_id,
            "test_point": list(test_point),
            "training_point": list(training_point),
            "release_id": release_id,
        }
        withdrawn.append(booklet_id)
    return withdrawn


# ---------------------------------------------------------------------------
# Initialisation


def initialize(config_path, project_path, view_map):
    """Create one benchmark project from a configuration and its view files.

    ``view_map`` maps each configured ``dataset_id`` to one existing ``view``
    export path.  Every view must exist exactly once, its point table is
    re-fingerprinted from the raw rows, and all three endpoint fields must
    match the configuration.  The original files are only read here: after
    initialisation the project must stand on its own.
    """
    document = io.load(config_path, MAX_REQUEST_BYTES)
    fields(
        document,
        ("schema_version", "kind", "project_id", "datasets", "relations"),
        where="benchmark config",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported benchmark config schema_version")
    if document["kind"] != CONFIG_KIND:
        raise ValueError("not a camera-tracks benchmark config")
    project_id = ident(document["project_id"], "project_id")

    entries = array(document["datasets"], 1, MAX_VIEWS, "config datasets")
    declared = {}
    for index, entry in enumerate(entries):
        where = f"config dataset {index}"
        fields(entry, ("dataset_id", "version", "fingerprint"), where=where)
        dataset_id = ident(entry["dataset_id"], f"{where} dataset_id")
        if dataset_id in declared:
            raise ValueError(f"duplicate dataset_id in config: {dataset_id}")
        declared[dataset_id] = {
            "dataset_id": dataset_id,
            "version": integer(entry["version"], 0, MAX_VERSIONS, f"{where} version"),
            "fingerprint": _digest(entry["fingerprint"], f"{where} fingerprint"),
        }

    bound = set(view_map)
    configured = set(declared)
    missing = sorted(configured - bound)
    extra = sorted(bound - configured)
    if missing:
        raise ValueError(f"missing view file for dataset(s): {', '.join(missing)}")
    if extra:
        raise ValueError(
            "view file supplied for dataset(s) without a config entry: "
            + ", ".join(extra)
        )

    datasets = {}
    total = 0
    for dataset_id in sorted(declared):
        loaded = _view_point_table(dataset_id, io.load(view_map[dataset_id],
                                   io.MAX_STORE_BYTES), declared[dataset_id])
        total += sum(len(batch["points"]) for batch in loaded["batches"].values())
        if total > MAX_PROJECT_POINTS:
            raise ValueError(
                f"project point table exceeds {MAX_PROJECT_POINTS} points"
            )
        datasets[dataset_id] = loaded

    relation_docs = array(
        document["relations"], 0, MAX_PROJECT_RELATIONS, "config relations"
    )
    relations = []
    seen = set()
    for index, entry in enumerate(relation_docs):
        fields(entry, ("points",), where=f"config relation {index}")
        raw = exact_array(entry["points"], 2, f"config relation {index} points")
        first = _point_ref(raw[0], f"config relation {index} first")
        second = _point_ref(raw[1], f"config relation {index} second")
        if first == second:
            raise ValueError(f"config relation {index} must join two different points")
        pair = tuple(sorted((first, second)))
        if pair in seen:
            raise ValueError(f"config relation {index} repeats an endpoint pair")
        seen.add(pair)
        relations.append(pair)

    project = {
        "schema_version": SCHEMA_VERSION,
        "kind": PROJECT_KIND,
        "project_id": project_id,
        "revision": 0,
        "datasets": datasets,
        "relations": [
            [list(ref) for ref in pair] for pair in sorted(
                relations,
                key=lambda pair: (_ref_sort_key(pair[0]), _ref_sort_key(pair[1])),
            )
        ],
        "releases": {},
        "booklets": {},
        "operations": {},
    }
    _verify_relations(
        project,
        [_stored_pair(pair, "stored relation") for pair in project["relations"]],
    )
    if len(project["relations"]) > MAX_PROJECT_RELATIONS:
        raise ValueError(
            f"project relations exceed {MAX_PROJECT_RELATIONS} entries"
        )
    validate_project(project)
    target = Path(project_path)
    if target.exists() or target.is_symlink():
        raise ValueError(f"project already exists: {project_path}")
    checkpoint(BENCH_BEFORE_COMMIT)
    io.publish_new(project_path, io.dump_store(project))
    return {
        "project_id": project["project_id"],
        "revision": project["revision"],
        "datasets": len(project["datasets"]),
        "points": _total_points(project),
        "relations": len(project["relations"]),
    }


def _verify_relations(project, relations):
    """Reject any relation whose endpoints are unknown or repeated."""
    seen = set()
    for pair in relations:
        if pair[0] == pair[1]:
            raise ValueError("relation must join two different points")
        for ref in pair:
            if not _point_exists(project, ref):
                raise ValueError(
                    f"relation references an unknown point: {_ref_json(ref)}"
                )
        if pair in seen:
            raise ValueError("relation endpoint pair is already registered")
        seen.add(pair)


# ---------------------------------------------------------------------------
# Stored project validation


def load_project(path):
    return validate_project(io.decode(io.read_bytes(path)))


def validate_project(document):
    fields(
        document,
        (
            "schema_version",
            "kind",
            "project_id",
            "revision",
            "datasets",
            "relations",
            "releases",
            "booklets",
            "operations",
        ),
        where="benchmark project",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported benchmark project schema_version")
    if document["kind"] != PROJECT_KIND:
        raise ValueError("not a camera-tracks benchmark project")
    ident(document["project_id"], "project_id")
    revision = integer(document["revision"], 0, MAX_VERSIONS, "revision")

    datasets = document["datasets"]
    if not isinstance(datasets, dict) or not 1 <= len(datasets) <= MAX_VIEWS:
        raise ValueError("project datasets must hold 1..8 entries")
    total = 0
    for dataset_id, dataset in datasets.items():
        ident(dataset_id, "project dataset_id")
        fields(dataset, ("version", "fingerprint", "batches"), where=f"dataset {dataset_id}")
        integer(dataset["version"], 0, MAX_VERSIONS, f"dataset {dataset_id} version")
        _digest(dataset["fingerprint"], f"dataset {dataset_id} fingerprint")
        batches = dataset["batches"]
        if not isinstance(batches, dict) or not batches:
            raise ValueError(f"dataset {dataset_id} batches must be a non-empty object")
        table = {}
        for key, batch in batches.items():
            if not isinstance(key, str) or not key.isdigit() or key != str(int(key)):
                raise ValueError("batch keys must be canonical decimal sequence strings")
            sequence = integer(int(key), 0, MAX_SEQUENCE, f"dataset {dataset_id} sequence")
            fields(batch, ("sequence", "points"), where=f"dataset {dataset_id} batch {key}")
            if batch["sequence"] != sequence:
                raise ValueError("dataset batch sequence key does not match value")
            points = batch["points"]
            if not isinstance(points, dict) or not points:
                raise ValueError(f"dataset {dataset_id} batch {key} must hold points")
            for track_id, xyz in points.items():
                ident(track_id, "stored track_id")
                exact_array(xyz, 3, f"point {dataset_id}/{key}/{track_id}")
                for cell in xyz:
                    rational(cell, f"point {dataset_id}/{key}/{track_id}")
                table[point_key(sequence, track_id)] = list(xyz)
        total += len(table)
        if total > MAX_PROJECT_POINTS:
            raise ValueError(f"project point table exceeds {MAX_PROJECT_POINTS} points")
        if fingerprint_points(table) != dataset["fingerprint"]:
            raise ValueError(f"dataset {dataset_id} fingerprint does not match its points")

    relations = document["relations"]
    if not isinstance(relations, list) or len(relations) > MAX_PROJECT_RELATIONS:
        raise ValueError(f"project relations must hold at most {MAX_PROJECT_RELATIONS} entries")
    seen = set()
    last_key = None
    for index, entry in enumerate(relations):
        pair = _stored_pair(entry, f"stored relation {index}")
        for ref in pair:
            if not _point_exists(document, ref):
                raise ValueError(
                    f"stored relation references an unknown point: {_ref_json(ref)}"
                )
        key = (_ref_sort_key(pair[0]), _ref_sort_key(pair[1]))
        if last_key is not None and key <= last_key:
            raise ValueError("stored relations must be unique and sorted")
        last_key = key
        seen.add(pair)

    releases = document["releases"]
    if not isinstance(releases, dict) or len(releases) > MAX_MATERIALS:
        raise ValueError("project releases must be an object within the material bound")
    for release_id, release in releases.items():
        ident(release_id, "stored release_id")
        fields(release, ("op_id", "revision", "points"), where=f"release {release_id}")
        ident(release["op_id"], f"release {release_id} op_id")
        integer(release["revision"], 1, MAX_VERSIONS, f"release {release_id} revision")
        _stored_ref_list(release["points"], f"release {release_id} points", MAX_PROJECT_POINTS)

    booklets = document["booklets"]
    if not isinstance(booklets, dict) or len(booklets) > MAX_MATERIALS:
        raise ValueError("project booklets must be an object within the material bound")
    for booklet_id, booklet in booklets.items():
        ident(booklet_id, "stored booklet_id")
        fields(
            booklet,
            (
                "op_id", "revision", "elevation_limit", "points", "sha256",
                "withdrawal", "scores",
            ),
            where=f"booklet {booklet_id}",
        )
        ident(booklet["op_id"], f"booklet {booklet_id} op_id")
        integer(booklet["revision"], 1, MAX_VERSIONS, f"booklet {booklet_id} revision")
        _stored_limit(
            booklet["elevation_limit"], f"booklet {booklet_id} elevation_limit"
        )
        booklet_points = _stored_ref_list(
            booklet["points"], f"booklet {booklet_id} points", MAX_BOOKLET_POINTS
        )
        for ref in booklet_points:
            if not _point_exists(document, ref):
                raise ValueError(
                    f"booklet {booklet_id} references an unknown point: {_ref_json(ref)}"
                )
        _digest(booklet["sha256"], f"booklet {booklet_id} sha256")
        score_ids = booklet["scores"]
        if not isinstance(score_ids, list):
            raise ValueError(f"booklet {booklet_id} scores must be an array")
        for score_id in score_ids:
            ident(score_id, f"booklet {booklet_id} score op_id")
        if len(set(score_ids)) != len(score_ids):
            raise ValueError(f"booklet {booklet_id} repeats a score operation")
        withdrawal = booklet["withdrawal"]
        if withdrawal is not None:
            fields(
                withdrawal,
                ("revision", "op_id", "test_point", "training_point", "release_id"),
                where=f"booklet {booklet_id} withdrawal",
            )
            integer(
                withdrawal["revision"], 1, MAX_VERSIONS,
                f"booklet {booklet_id} withdrawal revision",
            )
            ident(withdrawal["op_id"], f"booklet {booklet_id} withdrawal op_id")
            ident(withdrawal["release_id"], f"booklet {booklet_id} withdrawal release_id")
            test_point = _stored_ref(
                withdrawal["test_point"], f"booklet {booklet_id} withdrawal test_point"
            )
            training_point = _stored_ref(
                withdrawal["training_point"], f"booklet {booklet_id} withdrawal training_point"
            )
            if not _point_exists(document, test_point):
                raise ValueError("withdrawal test point is not part of the project")
            if not _point_exists(document, training_point):
                raise ValueError("withdrawal training point is not part of the project")

    operations = document["operations"]
    if not isinstance(operations, dict) or len(operations) > MAX_MATERIALS:
        raise ValueError("project operations must be an object within the operation bound")
    counts = {KIND_RELEASE: 0, KIND_ISSUE: 0, KIND_RELATE: 0, KIND_SCORE: 0}
    for op_id, operation in operations.items():
        ident(op_id, "stored op_id")
        fields(operation, ("kind", "digest", "receipt"), where=f"operation {op_id}")
        if operation["kind"] not in counts:
            raise ValueError(f"operation {op_id} has an unknown kind")
        _digest(operation["digest"], f"operation {op_id} digest")
        if not isinstance(operation["receipt"], dict):
            raise ValueError(f"operation {op_id} receipt must be an object")
        integer(
            operation["receipt"].get("revision", 0), 0, MAX_VERSIONS,
            f"operation {op_id} receipt revision",
        )
        counts[operation["kind"]] += 1
    if counts[KIND_RELEASE] != len(releases):
        raise ValueError("stored releases disagree with release operations")
    if counts[KIND_ISSUE] != len(booklets):
        raise ValueError("stored booklets disagree with issue operations")
    if revision != len(operations):
        raise ValueError("revision must equal the number of registered operations")

    # Every stored material and score must point back at the operation that
    # created it, and the operation must agree on the identity and revision.
    for release_id, release in releases.items():
        operation = operations.get(release["op_id"])
        if operation is None or operation["kind"] != KIND_RELEASE:
            raise ValueError(f"release {release_id} has no release operation")
        receipt = operation["receipt"]
        if receipt.get("release_id") != release_id:
            raise ValueError(f"release {release_id} op receipt names another release")
        if receipt.get("revision") != release["revision"]:
            raise ValueError(f"release {release_id} revision disagrees with its receipt")
        for ref in release["points"]:
            if not _point_exists(document, ref):
                raise ValueError(
                    f"release {release_id} references an unknown point: {_ref_json(ref)}"
                )
    referenced_scores = set()
    for booklet_id, booklet in booklets.items():
        operation = operations.get(booklet["op_id"])
        if operation is None or operation["kind"] != KIND_ISSUE:
            raise ValueError(f"booklet {booklet_id} has no issue operation")
        receipt = operation["receipt"]
        if receipt.get("booklet_id") != booklet_id:
            raise ValueError(f"booklet {booklet_id} op receipt names another booklet")
        if receipt.get("revision") != booklet["revision"]:
            raise ValueError(f"booklet {booklet_id} revision disagrees with its receipt")
        if receipt.get("sha256") != booklet["sha256"]:
            raise ValueError(f"booklet {booklet_id} digest disagrees with its receipt")
        for score_id in booklet["scores"]:
            score_op = operations.get(score_id)
            if score_op is None or score_op["kind"] != KIND_SCORE:
                raise ValueError(f"booklet {booklet_id} cites a non-score operation")
            if score_op["receipt"].get("booklet_id") != booklet_id:
                raise ValueError(
                    f"score operation {score_id} is filed under another booklet"
                )
            if score_id in referenced_scores:
                raise ValueError(f"score operation {score_id} is filed twice")
            referenced_scores.add(score_id)
        withdrawal = booklet["withdrawal"]
        if withdrawal is not None:
            causing = operations.get(withdrawal["op_id"])
            if causing is None or causing["kind"] not in (KIND_RELEASE, KIND_RELATE):
                raise ValueError(
                    f"booklet {booklet_id} withdrawal cites an unknown operation"
                )
            if causing["receipt"].get("revision") != withdrawal["revision"]:
                raise ValueError(
                    f"booklet {booklet_id} withdrawal revision disagrees with its operation"
                )
            if booklet_id not in causing["receipt"].get("withdrawn", []):
                raise ValueError(
                    f"withdrawal operation did not list booklet {booklet_id}"
                )
            if withdrawal["release_id"] not in releases:
                raise ValueError(
                    f"booklet {booklet_id} withdrawal cites an unknown release"
                )
            if _stored_ref(withdrawal["training_point"], "training_point") not in [
                _stored_ref(ref, "release point")
                for ref in releases[withdrawal["release_id"]]["points"]
            ]:
                raise ValueError(
                    f"booklet {booklet_id} training evidence is not in its release"
                )
    score_operations = {op_id for op_id, op in operations.items()
                        if op["kind"] == KIND_SCORE}
    if score_operations != referenced_scores:
        raise ValueError("stored scores and score operations are inconsistent")
    return document


def _stored_ref_list(value, where, high):
    if not isinstance(value, list) or not 1 <= len(value) <= high:
        raise ValueError(f"{where} must hold 1..{high} references")
    refs = [_stored_ref(entry, f"{where} entry") for entry in value]
    if len(set(refs)) != len(refs):
        raise ValueError(f"{where} must be distinct")
    if refs != sorted(refs, key=_ref_sort_key):
        raise ValueError(f"{where} must be sorted")
    return refs


# ---------------------------------------------------------------------------
# Package encodings (independent from the native view format)


def training_package(project, release_id):
    """Deterministic training-material bytes for one registered release.

    Only the listed points are delivered, with their complete XYZ and source
    endpoint.  Points merely connected to them are *not* included here, even
    though they count as exposed for benchmark selection.
    """
    release = project["releases"][release_id]
    rows = []
    for ref in release["points"]:
        rows.append({
            "dataset_id": ref[0],
            "sequence": ref[1],
            "track_id": ref[2],
            "xyz": list(_point_xyz(project, ref)),
            "source": _endpoint(project, ref),
        })
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": TRAINING_KIND,
        "package_format": TRAINING_FORMAT,
        "project_id": project["project_id"],
        "release_id": release_id,
        "release_revision": release["revision"],
        "count": len(rows),
        "points": rows,
    }
    return canonical_json(document)


def booklet_package(project, booklet_id):
    """Deterministic booklet bytes: identities, XY, source, no true Z."""
    booklet = project["booklets"][booklet_id]
    rows = []
    for ref in booklet["points"]:
        xyz = _point_xyz(project, ref)
        rows.append({
            "dataset_id": ref[0],
            "sequence": ref[1],
            "track_id": ref[2],
            "xy": [xyz[0], xyz[1]],
            "source": _endpoint(project, ref),
        })
    # The issued bytes are a historical artifact: withdrawal never alters
    # them, so a re-export after a withdrawal is byte-for-byte identical.
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": BOOKLET_KIND,
        "package_format": BOOKLET_FORMAT,
        "project_id": project["project_id"],
        "booklet_id": booklet_id,
        "booklet_revision": booklet["revision"],
        "elevation_limit": booklet["elevation_limit"],
        "count": len(rows),
        "points": rows,
    }
    return canonical_json(document)


def _export_checked(project, kind, material_id, output_path, project_path):
    target = Path(output_path)
    if target.resolve() == Path(project_path).resolve():
        raise ValueError("export target must differ from the project state")
    if target.exists() or target.is_symlink():
        raise ValueError(f"export target already exists: {output_path}")
    if kind == "training":
        payload = training_package(project, material_id)
    else:
        payload = booklet_package(project, material_id)
    checkpoint(BENCH_EXPORT_BEFORE_WRITE)
    io.publish_new(output_path, payload)
    return {
        "project_id": project["project_id"],
        "kind": kind,
        "id": material_id,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "output": str(output_path),
    }


# ---------------------------------------------------------------------------
# Operation registry and the four write operations


def _write_project(path, project):
    validate_project(project)
    checkpoint(BENCH_BEFORE_COMMIT)
    io.replace_atomic(path, io.dump_store(project))
    checkpoint(BENCH_AFTER_COMMIT)


def _existing_receipt(project, op_id, digest):
    existing = project["operations"].get(op_id)
    if existing is None:
        return None
    if existing["digest"] != digest:
        raise ValueError(
            f"op_id {op_id!r} was already used for a different request"
        )
    return existing["receipt"]


def _record_operation(project, op_id, kind, digest, receipt):
    project["operations"][op_id] = {
        "kind": kind,
        "digest": digest,
        "receipt": receipt,
    }


def _commit_revision(project):
    project["revision"] += 1
    return project["revision"]


def release(project_path, request_path):
    project = load_project(project_path)
    document = io.load(request_path, MAX_REQUEST_BYTES)
    op_id, canonical, parsed = _parse_release_request(document)
    digest = _digest_of(canonical)

    replay = _existing_receipt(project, op_id, digest)
    if replay is not None:
        return replay

    release_id = parsed["release_id"]
    if release_id in project["releases"]:
        raise ValueError(f"release_id {release_id!r} is already registered")
    for ref in parsed["points"]:
        if not _point_exists(project, ref):
            raise ValueError(
                f"release references an unknown point: {_ref_json(ref)}"
            )

    revision = _commit_revision(project)
    project["releases"][release_id] = {
        "op_id": op_id,
        "revision": revision,
        "points": [list(ref) for ref in parsed["points"]],
    }
    withdrawn = _withdraw_booklets(project, op_id, revision)
    payload = training_package(project, release_id)
    receipt = {
        "project_id": project["project_id"],
        "status": "released",
        "op_id": op_id,
        "revision": revision,
        "release_id": release_id,
        "points": len(parsed["points"]),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "withdrawn": withdrawn,
    }
    _record_operation(project, op_id, KIND_RELEASE, digest, receipt)
    _write_project(project_path, project)
    return receipt


def issue(project_path, request_path):
    project = load_project(project_path)
    document = io.load(request_path, MAX_REQUEST_BYTES)
    op_id, canonical, parsed = _parse_issue_request(document)
    digest = _digest_of(canonical)

    replay = _existing_receipt(project, op_id, digest)
    if replay is not None:
        return replay

    booklet_id = parsed["booklet_id"]
    if booklet_id in project["booklets"]:
        raise ValueError(f"booklet_id {booklet_id!r} is already registered")
    exposure = _exposure(project)
    for ref in parsed["points"]:
        if not _point_exists(project, ref):
            raise ValueError(f"booklet references an unknown point: {_ref_json(ref)}")
        if _point_exposed(exposure, ref):
            raise ValueError(
                "booklet contains an exposed point and cannot be issued: "
                + str(_ref_json(ref))
            )

    revision = _commit_revision(project)
    project["booklets"][booklet_id] = {
        "op_id": op_id,
        "revision": revision,
        "elevation_limit": parsed["elevation_limit"],
        "points": [list(ref) for ref in parsed["points"]],
        "sha256": None,
        "withdrawal": None,
        "scores": [],
    }
    # The issued bytes are frozen; the digest stored here is what every later
    # export must reproduce byte for byte.
    payload = booklet_package(project, booklet_id)
    project["booklets"][booklet_id]["sha256"] = hashlib.sha256(payload).hexdigest()
    receipt = {
        "project_id": project["project_id"],
        "status": "issued",
        "op_id": op_id,
        "revision": revision,
        "booklet_id": booklet_id,
        "points": len(parsed["points"]),
        "elevation_limit": parsed["elevation_limit"],
        "sha256": project["booklets"][booklet_id]["sha256"],
    }
    _record_operation(project, op_id, KIND_ISSUE, digest, receipt)
    _write_project(project_path, project)
    return receipt


def relate(project_path, request_path):
    project = load_project(project_path)
    document = io.load(request_path, MAX_REQUEST_BYTES)
    op_id, canonical, parsed = _parse_relate_request(document)
    digest = _digest_of(canonical)

    replay = _existing_receipt(project, op_id, digest)
    if replay is not None:
        return replay

    existing = {
        _stored_pair(pair, "stored relation") for pair in project["relations"]
    }
    if len(existing) + len(parsed["pairs"]) > MAX_PROJECT_RELATIONS:
        raise ValueError(
            f"project relations would exceed {MAX_PROJECT_RELATIONS} entries"
        )
    for pair in parsed["pairs"]:
        for ref in pair:
            if not _point_exists(project, ref):
                raise ValueError(
                    f"relation references an unknown point: {_ref_json(ref)}"
                )
        if pair in existing:
            raise ValueError(
                "relation endpoint pair is already registered: "
                + str(_pair_json(pair))
            )

    revision = _commit_revision(project)
    merged = existing | set(parsed["pairs"])
    project["relations"] = [
        [list(ref) for ref in pair] for pair in sorted(
            merged, key=lambda pair: (_ref_sort_key(pair[0]), _ref_sort_key(pair[1]))
        )
    ]
    withdrawn = _withdraw_booklets(project, op_id, revision)
    receipt = {
        "project_id": project["project_id"],
        "status": "related",
        "op_id": op_id,
        "revision": revision,
        "relations": len(parsed["pairs"]),
        "total_relations": len(project["relations"]),
        "withdrawn": withdrawn,
    }
    _record_operation(project, op_id, KIND_RELATE, digest, receipt)
    _write_project(project_path, project)
    return receipt


def score(project_path, request_path):
    project = load_project(project_path)
    document = io.load(request_path, MAX_REQUEST_BYTES)
    op_id, canonical, parsed = _parse_score_request(document)
    digest = _digest_of(canonical)

    replay = _existing_receipt(project, op_id, digest)
    if replay is not None:
        return replay

    booklet_id = parsed["booklet_id"]
    booklet = project["booklets"].get(booklet_id)
    if booklet is None:
        raise ValueError(f"unknown booklet_id: {booklet_id}")
    if booklet["withdrawal"] is not None:
        raise ValueError(
            f"booklet {booklet_id!r} is withdrawn; new predictions are refused"
        )
    if parsed["booklet_sha256"] != booklet["sha256"]:
        raise ValueError("booklet_sha256 does not match the issued booklet file")

    expected = {tuple(ref) for ref in booklet["points"]}
    supplied = {ref for ref, _value in parsed["predictions"]}
    if supplied != expected:
        missing = sorted(expected - supplied, key=_ref_sort_key)
        extra = sorted(supplied - expected, key=_ref_sort_key)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(str(_ref_json(r)) for r in missing[:5]))
        if extra:
            detail.append("unexpected " + ", ".join(str(_ref_json(r)) for r in extra[:5]))
        raise ValueError("predictions must cover every booklet point exactly once: "
                         + "; ".join(detail))

    prediction_id = parsed["prediction_id"]
    for other_id in booklet["scores"]:
        other = project["operations"][other_id]["receipt"]
        if other["prediction_id"] == prediction_id:
            raise ValueError(
                f"prediction_id {prediction_id!r} is already used in this booklet"
            )

    limit = Fraction(booklet["elevation_limit"])
    passed = 0
    for ref, predicted in parsed["predictions"]:
        actual = Fraction(_point_xyz(project, ref)[2])
        if abs(predicted - actual) <= limit:
            passed += 1
    total = len(parsed["predictions"])

    revision = _commit_revision(project)
    receipt = {
        "project_id": project["project_id"],
        "status": "scored",
        "op_id": op_id,
        "revision": revision,
        "booklet_id": booklet_id,
        "prediction_id": prediction_id,
        "passed": passed,
        "total": total,
    }
    booklet["scores"].append(op_id)
    _record_operation(project, op_id, KIND_SCORE, digest, receipt)
    _write_project(project_path, project)
    return receipt


# ---------------------------------------------------------------------------
# Read-only queries


def _booklet_state(project, booklet_id):
    booklet = project["booklets"].get(booklet_id)
    if booklet is None:
        raise ValueError(f"unknown booklet_id: {booklet_id}")
    return booklet


def scores(project_path, booklet_id=None):
    """Current comparable scores.

    Withdrawn booklets are dropped entirely from this table.  Audit queries
    use :func:`audit` instead and still see their historical scores.
    """
    project = load_project(project_path)
    rows = []
    for current in sorted(project["booklets"]):
        booklet = project["booklets"][current]
        if booklet_id is not None and current != booklet_id:
            continue
        if booklet["withdrawal"] is not None:
            continue
        for op_id in booklet["scores"]:
            receipt = project["operations"][op_id]["receipt"]
            rows.append({
                "booklet_id": current,
                "prediction_id": receipt["prediction_id"],
                "passed": receipt["passed"],
                "total": receipt["total"],
                "revision": receipt["revision"],
            })
    if booklet_id is not None:
        booklet = _booklet_state(project, booklet_id)
        return {
            "project_id": project["project_id"],
            "booklet_id": booklet_id,
            "scores": rows,
            "comparable": booklet["withdrawal"] is None,
        }
    return {"project_id": project["project_id"], "revision": project["revision"],
            "scores": rows}


def audit(project_path, booklet_id=None):
    """Historical predictions and each booklet's current withdrawal status.

    Original scores are returned unchanged, even for withdrawn booklets;
    they are never zeroed and never mixed into :func:`scores`.
    """
    project = load_project(project_path)
    rows = []
    for current in sorted(project["booklets"]):
        booklet = project["booklets"][current]
        if booklet_id is not None and current != booklet_id:
            continue
        withdrawal = booklet["withdrawal"]
        entry = {
            "booklet_id": current,
            "revision": booklet["revision"],
            "points": len(booklet["points"]),
            "withdrawn": withdrawal is not None,
            "withdrawal": None,
            "predictions": [],
        }
        if withdrawal is not None:
            entry["withdrawal"] = {
                "revision": withdrawal["revision"],
                "op_id": withdrawal["op_id"],
                "test_point": _ref_json(tuple(withdrawal["test_point"])),
                "training_point": _ref_json(tuple(withdrawal["training_point"])),
                "release_id": withdrawal["release_id"],
            }
        for op_id in booklet["scores"]:
            receipt = project["operations"][op_id]["receipt"]
            entry["predictions"].append({
                "prediction_id": receipt["prediction_id"],
                "passed": receipt["passed"],
                "total": receipt["total"],
                "revision": receipt["revision"],
                "op_id": op_id,
            })
        rows.append(entry)
    if booklet_id is not None:
        if not rows:
            raise ValueError(f"unknown booklet_id: {booklet_id}")
        return {"project_id": project["project_id"], "booklet": rows[0]}
    return {"project_id": project["project_id"], "revision": project["revision"],
            "booklets": rows}


def status(project_path):
    project = load_project(project_path)
    booklets = []
    for booklet_id in sorted(project["booklets"]):
        booklet = project["booklets"][booklet_id]
        booklets.append({
            "booklet_id": booklet_id,
            "withdrawn": booklet["withdrawal"] is not None,
            "comparable": booklet["withdrawal"] is None,
        })
    return {
        "project_id": project["project_id"],
        "revision": project["revision"],
        "datasets": sorted(project["datasets"]),
        "points": _total_points(project),
        "relations": len(project["relations"]),
        "releases": sorted(project["releases"]),
        "booklets": booklets,
    }


def export(project_path, kind, material_id, output_path):
    project = load_project(project_path)
    if kind == "training":
        if material_id not in project["releases"]:
            raise ValueError(f"unknown release_id: {material_id}")
    elif kind == "booklet":
        if material_id not in project["booklets"]:
            raise ValueError(f"unknown booklet_id: {material_id}")
    else:
        raise ValueError("export kind must be 'training' or 'booklet'")
    return _export_checked(project, kind, material_id, output_path, project_path)
