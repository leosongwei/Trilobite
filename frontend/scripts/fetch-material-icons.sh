#!/usr/bin/env bash
# Fetch the Material Symbols Outlined icon font from Google Fonts and subset
# it to only the icons used by the frontend.
#
# Why subset: the full variable font is ~4 MB; the app uses a handful of
# icons, so the subset is a few KB.
#
# Output (gitignored, generated artifacts):
#   frontend/public/vendor/material-symbols/material-symbols-outlined.woff2
#   frontend/public/vendor/material-symbols/LICENSE  (Apache-2.0, required
#       alongside the redistributed font)
#
# Requirements: curl, uv (for a throwaway fonttools environment).
#
# Re-run this script whenever you add an icon below.

set -euo pipefail
cd "$(dirname "$0")/.."

FAMILY="Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200"
# A modern UA makes Google Fonts return the full font as a single file
# instead of unicode-range slices.
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
OUT="public/vendor/material-symbols"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Icons used by the frontend, as <name>=<codepoint> pairs. Codepoints are the
# PUA mappings of the Material Symbols variable font (see frontend/src/icons.css
# for the matching .ms-* classes).
ICONS=(
  menu=e5d2        # App.vue sidebar menu toggle
  arrow_back=e5c4  # App.vue / FileManager.vue "back" buttons
  schedule=efd6    # scheduled-agent badges
  stop=e047        # ChatInput.vue stop button
  close=e5cd       # SessionSidebar.vue delete/cancel buttons
  check=e668       # SessionSidebar.vue save button
  edit=f097        # SessionSidebar.vue rename button
  expand_more=e5cf # FileTree.vue tree arrow
  folder=e2c7      # FileTree.vue directory rows
  description=e873 # FileTree.vue file rows
)

echo "==> Resolving font URL from Google Fonts CSS"
CSS="$(curl -fsSL -A "$UA" "https://fonts.googleapis.com/css2?family=$FAMILY")"
URL="$(printf '%s' "$CSS" | grep -oP 'url\(\K[^)]+' | head -1)"
[ -n "$URL" ] || { echo "error: could not extract font URL"; exit 1; }
echo "    $URL"

echo "==> Downloading full variable font"
curl -fsSL "$URL" -o "$TMP/full.woff2"

echo "==> Subsetting to $((${#ICONS[@]})) icons"
UNICODES="$(printf '%s' "${ICONS[@]}" | sed 's/=/ /g; s/ /,/g')"
# unicodes must be comma-separated U+XXXX; build from the codepoint column.
CP="$(printf '%s\n' "${ICONS[@]}" | cut -d= -f2 | sed 's/^/U+/' | paste -sd,)"
uv run --with fonttools --with brotli pyftsubset \
  "$TMP/full.woff2" \
  --unicodes="$CP" \
  --layout-features='' \
  --flavor=woff2 \
  --output-file="$TMP/subset.woff2"

mkdir -p "$OUT"
mv "$TMP/subset.woff2" "$OUT/material-symbols-outlined.woff2"

echo "==> Fetching Apache-2.0 license text"
curl -fsSL "https://www.apache.org/licenses/LICENSE-2.0.txt" -o "$OUT/LICENSE"

echo "==> Done:"
ls -l "$OUT"
