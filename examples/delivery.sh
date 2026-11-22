#!/bin/sh
# Run the whole offline delivery walkthrough from a clean temporary directory.
# Requires only Python 3.11+ with the standard library; no installation.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${1:-"$here/run/example"}
rm -rf "$work"
mkdir -p "$work"
cd "$work"

export PYTHONPATH="$here/src"
camera_tracks() { python3 -m camera_tracks.cli "$@"; }

# 1. Build the upstream store from the dataset document, then drop the input.
cp "$here/fixtures/build.json" build.json
camera_tracks build --input build.json --store upstream.json
rm build.json

# 2. Export and apply the full delivery of version 0.
camera_tracks export --store upstream.json --full --output full.json
camera_tracks apply --store downstream.json --package full.json

# 3. Correct the right camera for sequences 10..20.  The two affected points
#    keep a unique zero-residual solution but now have negative depth.
cp "$here/fixtures/correction.json" correction.json
camera_tracks correct --store upstream.json --request correction.json
camera_tracks query --store upstream.json --version 1 --sequence 10 --track marker

# 4. Export and apply the retraction delta.
camera_tracks export --store upstream.json --base-version 0 --output retract.json
camera_tracks apply --store downstream.json --package retract.json

# 5. Restore the right camera for 20..30 and deliver only the recovered point.
sed 's/"change_id": "flip-right-x-10-20"/"change_id": "restore-right-x-20-30"/;
     s/"base_version": 0/"base_version": 1/;
     s/"start": 10/"start": 20/;
     s/"end": 20/"end": 30/;
     s/"1", "0", "0", "1"/"1", "0", "0", "-1"/' \
    correction.json > restore.json
camera_tracks correct --store upstream.json --request restore.json
camera_tracks export --store upstream.json --base-version 1 --output restore-delta.json
camera_tracks apply --store downstream.json --package restore-delta.json

# 6. Retrying an already applied package answers with its original version.
camera_tracks apply --store downstream.json --package retract.json

# 7. Show the final downstream view.
camera_tracks view --store downstream.json --output view.json
echo "artifacts in $work"
