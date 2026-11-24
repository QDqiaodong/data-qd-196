#!/bin/sh
# Training-material / blind-booklet walkthrough (训练材料与盲测基准发布).
#
# Every view comes from the real build -> export -> apply -> view delivery
# chain, so the benchmark binds the same files the delivery pipeline emits.
# A0/A1 share a batch, B0/B1 share another batch, H and U each get their own.
# The script then issues H and U booklets, scores both (H exactly at the
# limit), releases A0 alone, proves H stays scoreable, appends late ties,
# and shows H withdrawn while U remains usable.
# Requires only Python 3.11+ with the standard library; no installation.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${1:-"$here/run/bench"}
rm -rf "$work"
mkdir -p "$work"
cd "$work"

export PYTHONPATH="$here/src"
camera_tracks() { python3 -m camera_tracks.cli "$@"; }

# --- 1. Build three datasets through the delivery chain -------------------
python3 - "$work" <<'PY'
import json
from pathlib import Path

work = Path(".")
left = {"id": "left",
        "projection": [["1","0","0","0"],["0","1","0","0"],["0","0","1","0"]]}
right = {"id": "right",
         "projection": [["1","0","0","-1"],["0","1","0","0"],["0","0","1","0"]]}

def obs(x, y, z):
    return [
        {"camera_id": "left",  "xy": [str(x / z), str(y / z)], "weight": "1"},
        {"camera_id": "right", "xy": [str((x - 1) / z), str(y / z)], "weight": "1"},
    ]

datasets = {
    # sequence 1 is one batch (A0, A1); sequence 2 is H's own batch.
    "dsA": [(1, "A0", (0, 0, 10)), (1, "A1", (1, 0, 10)), (2, "H", (5, 5, 20))],
    # one batch holding B0 and B1.
    "dsB": [(7, "B0", (2, 2, 20)), (7, "B1", (3, 2, 20))],
    # U alone.
    "dsU": [(9, "U", (9, 9, 40))],
}
for dataset, points in datasets.items():
    batches = {}
    for sequence, track, xyz in points:
        batches.setdefault(sequence, []).append(
            {"id": track, "observations": obs(*xyz)}
        )
    build = {
        "dataset_id": dataset,
        "max_reprojection_error": "0",
        "cameras": [left, right],
        "batches": [
            {"sequence": sequence, "tracks": tracks}
            for sequence, tracks in sorted(batches.items())
        ],
    }
    (work / f"build-{dataset}.json").write_text(json.dumps(build), encoding="utf-8")
PY

for dataset in dsA dsB dsU; do
    camera_tracks build --input "build-$dataset.json" --store "up-$dataset.json"
    camera_tracks export --store "up-$dataset.json" --full --output "full-$dataset.json"
    camera_tracks apply --store "down-$dataset.json" --package "full-$dataset.json"
    camera_tracks view --store "down-$dataset.json" --output "view-$dataset.json"
done

# --- 2. Freeze one benchmark project -------------------------------------
python3 - "$work" <<'PY'
import json
from pathlib import Path

def view(dataset):
    return json.loads(Path(f"view-{dataset}.json").read_text(encoding="utf-8"))

config = {
    "schema_version": 1,
    "kind": "camera-tracks-benchmark-config",
    "project_id": "bench-walkthrough",
    "datasets": [
        {"dataset_id": dataset,
         "version": view(dataset)["version"],
         "fingerprint": view(dataset)["fingerprint"]}
        for dataset in ("dsA", "dsB", "dsU")
    ],
    "relations": [],
}
Path("config.json").write_text(json.dumps(config), encoding="utf-8")

def ref(dataset, sequence, track):
    return {"dataset_id": dataset, "sequence": sequence, "track_id": track}

def write(name, document):
    Path(name).write_text(json.dumps(document), encoding="utf-8")

write("issue-H.json", {
    "op_id": "op-issue-H", "booklet_id": "book-H",
    "elevation_limit": "1.5", "points": [ref("dsA", 2, "H")],
})
write("issue-U.json", {
    "op_id": "op-issue-U", "booklet_id": "book-U",
    "elevation_limit": "2", "points": [ref("dsU", 9, "U")],
})
write("issue-A1.json", {
    "op_id": "op-issue-A1", "booklet_id": "book-A1",
    "elevation_limit": "1", "points": [ref("dsA", 1, "A1")],
})
write("release-A0.json", {
    "op_id": "op-release-A0", "release_id": "train-A0",
    "points": [ref("dsA", 1, "A0")],
})
write("relate-A1-B0.json", {
    "op_id": "op-relate-A1-B0",
    "relations": [{"points": [ref("dsB", 7, "B0"), ref("dsA", 1, "A1")]}],
})
write("relate-B1-H.json", {
    "op_id": "op-relate-B1-H",
    "relations": [{"points": [ref("dsA", 2, "H"), ref("dsB", 7, "B1")]}],
})
write("release-U.json", {
    "op_id": "op-release-U", "release_id": "train-U",
    "points": [ref("dsU", 9, "U")],
})
PY

camera_tracks bench --config config.json --project project.json \
    --view dsA=view-dsA.json --view dsB=view-dsB.json --view dsU=view-dsU.json

# --- 3. Issue and score both booklets ------------------------------------
camera_tracks bench-issue --project project.json --request issue-H.json
camera_tracks bench-issue --project project.json --request issue-U.json
camera_tracks bench-export-booklet --project project.json --id book-H --output book-H.json
camera_tracks bench-export-booklet --project project.json --id book-U --output book-U.json

python3 - "$work" <<'PY'
import hashlib, json
from pathlib import Path

def ref(dataset, sequence, track, z):
    return {"dataset_id": dataset, "sequence": sequence, "track_id": track, "z": z}

sha_h = hashlib.sha256(Path("book-H.json").read_bytes()).hexdigest()
sha_u = hashlib.sha256(Path("book-U.json").read_bytes()).hexdigest()
# H's true Z is 20 and the limit is 1.5: 21.5 passes exactly at the limit.
Path("score-H-1.json").write_text(json.dumps({
    "op_id": "op-score-H-1", "booklet_id": "book-H", "booklet_sha256": sha_h,
    "prediction_id": "pred-H-1", "predictions": [ref("dsA", 2, "H", "21.5")],
}), encoding="utf-8")
Path("score-U-1.json").write_text(json.dumps({
    "op_id": "op-score-U-1", "booklet_id": "book-U", "booklet_sha256": sha_u,
    "prediction_id": "pred-U-1", "predictions": [ref("dsU", 9, "U", "40")],
}), encoding="utf-8")
PY

camera_tracks bench-score --project project.json --request score-H-1.json
camera_tracks bench-score --project project.json --request score-U-1.json

# --- 4. Release A0 only (no download) ------------------------------------
camera_tracks bench-release --project project.json --request release-A0.json
echo "H and U scores remain comparable after the release:"
camera_tracks bench-scores --project project.json
echo "issuing an A1-only booklet must be refused:"
if camera_tracks bench-issue --project project.json --request issue-A1.json; then
    echo "UNEXPECTED: A1 booklet was issued" >&2
    exit 1
else
    echo "  refused as expected"
fi

# --- 5. Drop the source views, then append late ties ---------------------
rm -f view-dsA.json view-dsB.json view-dsU.json config.json

camera_tracks bench-relate --project project.json --request relate-A1-B0.json
echo "H is still scoreable after A1-B0:"
python3 - "$work" <<'PY'
import hashlib, json
from pathlib import Path
sha_h = hashlib.sha256(Path("book-H.json").read_bytes()).hexdigest()
Path("score-H-2.json").write_text(json.dumps({
    "op_id": "op-score-H-2", "booklet_id": "book-H", "booklet_sha256": sha_h,
    "prediction_id": "pred-H-2",
    "predictions": [{"dataset_id": "dsA", "sequence": 2, "track_id": "H", "z": "20"}],
}), encoding="utf-8")
PY
camera_tracks bench-score --project project.json --request score-H-2.json

camera_tracks bench-relate --project project.json --request relate-B1-H.json
echo "H is now withdrawn; U is still usable:"
camera_tracks bench-scores --project project.json
camera_tracks bench-audit --project project.json --booklet book-H

# --- 6. Byte-identical re-export of a withdrawn booklet ------------------
camera_tracks bench-export-booklet --project project.json --id book-H --output book-H-again.json
cmp book-H.json book-H-again.json && echo "book-H re-export is byte-identical"
if grep -q '"z"' book-H.json; then
    echo "UNEXPECTED: booklet leaks a true Z" >&2
    exit 1
fi
echo "book-H carries no true Z"

# --- 7. A new release withdraws U; replaying old operations is stable ----
camera_tracks bench-release --project project.json --request release-U.json
camera_tracks bench-relate --project project.json --request relate-A1-B0.json
camera_tracks bench-relate --project project.json --request relate-B1-H.json
echo "final status:"
camera_tracks bench-status --project project.json

echo "artifacts in $work"
