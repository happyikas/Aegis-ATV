#!/usr/bin/env bash
# Build docs/build/WHITEPAPER.pdf from WHITEPAPER.md.
#
# Pipeline:
#   1. pandoc converts WHITEPAPER.md → body HTML (no <html>, no <head>)
#   2. We assemble cover.html + body HTML into a single page
#   3. Chrome headless prints the page to A4 PDF (CSS handles styling)
#
# Why this stack:
#   - Pandoc gives us robust markdown → HTML (Korean-safe, table-aware).
#   - Chrome headless renders Korean fonts natively (Apple SD Gothic Neo
#     on macOS) and respects every CSS @page rule including page numbers
#     and headers/footers.
#   - No LaTeX, no kotex, no font installation.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

MD="WHITEPAPER.md"
OUT_DIR="docs/build"
HTML="$OUT_DIR/whitepaper.html"
PDF="$OUT_DIR/WHITEPAPER.pdf"
CSS="tools/whitepaper/style.css"
COVER="tools/whitepaper/cover.html"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

mkdir -p "$OUT_DIR"

if [[ ! -f "$MD" ]]; then
  echo "❌ $MD not found"
  exit 1
fi

if [[ ! -x "$CHROME" ]]; then
  echo "❌ Google Chrome not found at: $CHROME"
  echo "   Set CHROME env var to its path."
  exit 1
fi

# 1. pandoc → body HTML fragment
BODY_HTML="$OUT_DIR/_body.html"
pandoc "$MD" \
  --from gfm \
  --to html5 \
  --output "$BODY_HTML" \
  --wrap=preserve

# 2. Assemble full HTML doc with cover + body
CSS_INLINE="$(cat "$CSS")"
COVER_HTML="$(cat "$COVER")"
BODY="$(cat "$BODY_HTML")"

cat > "$HTML" <<EOF
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<title>AegisData 기술 백서 v1.0</title>
<style>
${CSS_INLINE}
</style>
</head>
<body>
${COVER_HTML}
<div class="body">
${BODY}
</div>
</body>
</html>
EOF

echo "  ✓ assembled $HTML ($(wc -c < "$HTML" | tr -d ' ') bytes)"

# 3. Chrome headless → PDF
"$CHROME" \
  --headless=new \
  --disable-gpu \
  --no-pdf-header-footer \
  --print-to-pdf="$PDF" \
  --print-to-pdf-no-header \
  --no-margins \
  --hide-scrollbars \
  --virtual-time-budget=8000 \
  "file://$ROOT/$HTML" 2>&1 | grep -v -E "^$|gpu_command_buffer|GPU process" || true

if [[ ! -f "$PDF" ]]; then
  echo "❌ PDF generation failed"
  exit 1
fi

# Cleanup intermediates
rm -f "$BODY_HTML"

PDF_BYTES=$(wc -c < "$PDF" | tr -d ' ')
PDF_PAGES="?"
if command -v mdls >/dev/null 2>&1; then
  PDF_PAGES=$(mdls -name kMDItemNumberOfPages -raw "$PDF" 2>/dev/null || echo "?")
fi

echo
echo "  ✓ wrote $PDF"
echo "    size:  $PDF_BYTES bytes"
echo "    pages: $PDF_PAGES"
