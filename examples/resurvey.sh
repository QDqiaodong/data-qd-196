#!/bin/sh
# Resurvey work-order walkthrough: build one survey through the delivery
# chain, export the view, then generate the next round's resurvey work
# orders for one zone plan.  Requires only Python 3.11+ with the standard
# library; no installation.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${1:-"$here/run/resurvey"}
rm -rf "$work"
mkdir -p "$work"
cd "$work"

export PYTHONPATH="$here/src"
camera_tracks() { python3 -m camera_tracks.cli "$@"; }

# 1. Deliver the survey: three points at [2,1,4] (sequences 10, 20, 30).
cp "$here/fixtures/build.json" build.json
camera_tracks build --input build.json --store upstream.json
camera_tracks export --store upstream.json --full --output full.json
camera_tracks apply --store downstream.json --package full.json
camera_tracks view --store downstream.json --output view.json

# 2. The zone plan pins the exported endpoint (dataset, version,
#    fingerprint), a 4x4 unit grid over the origin, the elevation band
#    [0,10), three required batches per cell, one exempt passage cell and
#    one no-entry cell.  All three delivered points land in cell (2,1), so
#    that cell is accepted; the report opens one work order covering the
#    remaining 13 pending cells.
cp "$here/fixtures/resurvey-plan.json" plan.json
camera_tracks resurvey --view view.json --plan plan.json --output resurvey-report.json

echo "artifacts in $work"
