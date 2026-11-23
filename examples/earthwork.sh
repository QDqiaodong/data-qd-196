#!/bin/sh
# Earthwork walkthrough: build the old and new ground surveys through the
# delivery chain, export both views, then settle the work zone slice by
# slice.  The zone [0,0,3,1] has one old block [0,0,2,1] at elevation 0 and
# one new block [1,0,3,1] on the plane z = x - 3/2; only their 1 m overlap
# is settled, with 1/8 m^3 cut and 1/8 m^3 fill, and 2 m^2 remain unsettled.
# Requires only Python 3.11+ with the standard library; no installation.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${1:-"$here/run/earthwork"}
rm -rf "$work"
mkdir -p "$work"
cd "$work"

export PYTHONPATH="$here/src"
camera_tracks() { python3 -m camera_tracks.cli "$@"; }

for side in old new; do
    cp "$here/fixtures/earthwork-$side-build.json" "$side-build.json"
    camera_tracks build --input "$side-build.json" --store "$side-upstream.json"
    camera_tracks export --store "$side-upstream.json" --full --output "$side-full.json"
    camera_tracks apply --store "$side-downstream.json" --package "$side-full.json"
    camera_tracks view --store "$side-downstream.json" --output "$side-view.json"
done

# The plan pins both exported endpoints and declares the two differently
# blocked ground surfaces.  Its fingerprints were produced by this same
# delivery chain from the build fixtures above.
cp "$here/fixtures/earthwork-plan.json" plan.json
camera_tracks earthwork --old old-view.json --new new-view.json \
    --plan plan.json --output earthwork-package.json

echo "artifacts in $work"
