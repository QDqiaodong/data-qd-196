#!/bin/sh
# Offline origin merge (坐标归并) walkthrough.  Two fragment views are built
# through the real delivery chain, then merged under the acceptance triangle:
# R->A = 1 um, A->B = 1 um, R->B = 3 um (all tentative) plus the reviewed
# R->C = 10 um tie.  The first run withholds A and B; marking R->A reviewed
# releases A; setting R->B to 2 um makes the whole network consistent.
# Requires only Python 3.11+ with the standard library; no installation.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${1:-"$here/run/merge"}
rm -rf "$work"
mkdir -p "$work"
cd "$work"

export PYTHONPATH="$here/src"
camera_tracks() { python3 -m camera_tracks.cli "$@"; }

# Two datasets, each reconstructing [2,1,4] at sequence 10 through the full
# build/export/apply/view chain.  C also carries one point.
python3 - "$work" <<'PY'
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
left = {
    "id": "left",
    "projection": [["1", "0", "0", "0"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
}
right = {
    "id": "right",
    "projection": [["1", "0", "0", "-1"], ["0", "1", "0", "0"], ["0", "0", "1", "0"]],
}
obs = [
    {"camera_id": "left", "xy": ["0.5", "0.25"], "weight": "1"},
    {"camera_id": "right", "xy": ["0.25", "0.25"], "weight": "1"},
]
for dataset in ("dsR", "dsA", "dsC"):
    build = {
        "dataset_id": dataset,
        "max_reprojection_error": "0",
        "cameras": [left, right],
        "batches": [
            {"sequence": 10, "tracks": [{"id": "marker", "observations": obs}]}
        ],
    }
    (work / f"build-{dataset}.json").write_text(json.dumps(build), encoding="utf-8")
PY

for dataset in dsR dsA dsC; do
    camera_tracks build --input "build-$dataset.json" --store "up-$dataset.json"
    camera_tracks export --store "up-$dataset.json" --full --output "full-$dataset.json"
    camera_tracks apply --store "down-$dataset.json" --package "full-$dataset.json"
    camera_tracks view --store "down-$dataset.json" --output "view-$dataset.json"
done

# Build the three plan variants from the exported endpoints.
python3 - "$work" <<'PY'
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])


def view(dataset):
    return json.loads((work / f"view-{dataset}.json").read_text(encoding="utf-8"))


def endpoint(dataset):
    doc = view(dataset)
    return {
        "dataset_id": doc["dataset_id"],
        "version": doc["version"],
        "fingerprint": doc["fingerprint"],
    }


def link(record_id, u, v, delta, status):
    return {
        "record_id": record_id,
        "from_fragment": u,
        "to_fragment": v,
        "delta": list(delta),
        "status": status,
    }


def plan(name, ra_status, rb_delta):
    fragments = ["R", "A", "B", "C"]
    endpoints = {f: endpoint(f"ds{f}") for f in ("R", "A", "C")}
    doc = {
        "schema_version": 1,
        "kind": "camera-tracks-merge-plan",
        "plan_id": name,
        "baseline": "R",
        "fragments": [
            {"fragment_id": f, "endpoint": endpoints.get(f)} for f in fragments
        ],
        "links": [
            link("lRA", "R", "A", (1, 0, 0), ra_status),
            link("lAB", "A", "B", (1, 0, 0), "tentative"),
            link("lRB", "R", "B", rb_delta, "tentative"),
            link("lRC", "R", "C", (10, 0, 0), "reviewed"),
        ],
    }
    (work / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


plan("plan-open", "tentative", (3, 0, 0))
plan("plan-locked", "reviewed", (3, 0, 0))
plan("plan-consistent", "reviewed", (2, 0, 0))
PY

for variant in open locked consistent; do
    camera_tracks merge \
        --plan "plan-$variant.json" \
        --output "merge-$variant.json" \
        --view "R=view-dsR.json" \
        --view "A=view-dsA.json" \
        --view "C=view-dsC.json"
done

echo "artifacts in $work"
echo "open package withholds A/B; locked releases A; consistent releases all four"
