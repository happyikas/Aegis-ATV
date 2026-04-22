#!/usr/bin/env bash
# Build docs/build/PITCH_DECK.pdf — A4 landscape, 13 slides.
#
# No pandoc needed (deck is hand-authored HTML). Just Chrome
# headless to print the assembled HTML.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

HTML="tools/deck/deck.html"
OUT_DIR="docs/build"
PDF="$OUT_DIR/PITCH_DECK.pdf"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

mkdir -p "$OUT_DIR"

if [[ ! -f "$HTML" ]]; then
  echo "❌ $HTML not found"
  exit 1
fi

if [[ ! -x "$CHROME" ]]; then
  echo "❌ Google Chrome not found at: $CHROME"
  echo "   Set CHROME env var to its path."
  exit 1
fi

"$CHROME" \
  --headless=new \
  --disable-gpu \
  --no-pdf-header-footer \
  --print-to-pdf="$PDF" \
  --print-to-pdf-no-header \
  --no-margins \
  --hide-scrollbars \
  --virtual-time-budget=8000 \
  "file://$ROOT/$HTML" 2>&1 | grep -v -E "^$|gpu_command_buffer|GPU process|externally_managed|allocator multiple|os_integration" || true

if [[ ! -f "$PDF" ]]; then
  echo "❌ PDF generation failed"
  exit 1
fi

PDF_BYTES=$(wc -c < "$PDF" | tr -d ' ')
PDF_PAGES="?"
if command -v pdfinfo >/dev/null 2>&1; then
  PDF_PAGES=$(pdfinfo "$PDF" 2>/dev/null | awk '/^Pages:/ {print $2}')
fi

echo
echo "  ✓ wrote $PDF"
echo "    size:  $PDF_BYTES bytes"
echo "    pages: $PDF_PAGES"
