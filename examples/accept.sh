#!/bin/sh
# Resurvey displacement acceptance walkthrough: build two surveys through the
# delivery chain, export both views, then run the acceptance report twice
# (before and after one field confirmation).
# Requires only Python 3.11+ with the standard library; no installation.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${1:-"$here/run/accept"}
rm -rf "$work"
mkdir -p "$work"
cd "$work"

export PYTHONPATH="$here/src"
camera_tracks() { python3 -m camera_tracks.cli "$@"; }

# 1. Baseline survey: three points at [2,1,4] (sequences 10, 20, 30).
cp "$here/fixtures/build.json" base-build.json
camera_tracks build --input base-build.json --store base-upstream.json
camera_tracks export --store base-upstream.json --full --output base-full.json
camera_tracks apply --store base-downstream.json --package base-full.json
camera_tracks view --store base-downstream.json --output baseline-view.json

# 2. Resurvey: the same physical points, now at [3,1,4], [2,1,4], [9,1,4].
cp "$here/fixtures/accept-resurvey-build.json" re-build.json
camera_tracks build --input re-build.json --store re-upstream.json
camera_tracks export --store re-upstream.json --full --output re-full.json
camera_tracks apply --store re-downstream.json --package re-full.json
camera_tracks view --store re-downstream.json --output resurvey-view.json

# 3. Acceptance with the field candidates only.  Every complete
#    correspondence exceeds the limit on exactly one point, but which point
#    depends on the pairing: the zone cannot be signed off.
cp "$here/fixtures/accept-zone.json" zone.json
camera_tracks accept --baseline baseline-view.json --resurvey resurvey-view.json \
    --config zone.json --output acceptance-report.json

# 4. After the crew confirms the (30, marker) pair on site, the same run
#    pins the exceeded point down to sequence 30.
cp "$here/fixtures/accept-zone-confirmed.json" zone-confirmed.json
camera_tracks accept --baseline baseline-view.json --resurvey resurvey-view.json \
    --config zone-confirmed.json --output acceptance-report-confirmed.json

echo "artifacts in $work"
