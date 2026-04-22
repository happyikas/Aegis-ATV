#!/usr/bin/env bash
# Capture dashboard screenshots via headless Chrome.
#
# Pre-req: the service must be running on http://localhost:8000 with
# data already populated by `bash demo/record.sh`.
#
# Output: PNG files in demo/recording/screens/

set -euo pipefail

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT="$(cd "$(dirname "$0")" && pwd)/screens"
mkdir -p "$OUT"

# Common headless flags
COMMON=(
  --headless=new
  --disable-gpu
  --hide-scrollbars
  --window-size=1440,3200
  --virtual-time-budget=4000
  --no-sandbox
)

shoot() {
  local name="$1" url="$2"
  echo ">>> $name → $url"
  "$CHROME" "${COMMON[@]}" \
    --screenshot="$OUT/$name.png" \
    "$url"
}

shoot "01-dashboard-overview"  "http://localhost:8000"
shoot "02-theater"             "http://localhost:8000/theater"
shoot "03-attestation-json"    "http://localhost:8000/attestation"
shoot "04-replay-json"         "http://localhost:8000/forensic/replay"
shoot "05-ham-stats"           "http://localhost:8000/ham/stats"
shoot "06-burnin-status"       "http://localhost:8000/burnin-status"
shoot "07-admin-aid"           "http://localhost:8000/admin/aid"
shoot "08-openapi-docs"        "http://localhost:8000/docs"

echo
echo "captured $(ls "$OUT" | wc -l) frames into $OUT"
ls -lh "$OUT"
