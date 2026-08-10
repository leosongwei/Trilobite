#!/usr/bin/env bash
# Fetch the Material Symbols Outlined icon font from Google Fonts and subset
# it to only the icons used by the frontend.
#
# Why subset: the full variable font is ~4 MB; the app uses a handful of
# icons, so the subset is a few KB.
#
# The icon list lives in ONE place: frontend/public/icons.json (single source
# of truth). This script reads it to build the subset and verifies that the
# codepoints in frontend/src/icons.css match it; the debug page
# (frontend/public/debug-icons.html) reads the same file at runtime.
#
# Output (gitignored, generated artifacts):
#   frontend/public/vendor/material-symbols/material-symbols-outlined.woff2
#   frontend/public/vendor/material-symbols/LICENSE  (Apache-2.0, required
#       alongside the redistributed font)
#
# Requirements: curl, uv (for a throwaway fonttools environment), python3.
#
# Re-run this script whenever you add an icon to public/icons.json.

set -euo pipefail
cd "$(dirname "$0")/.."

FAMILY="Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200"
# A modern UA makes Google Fonts return the full font as a single file
# instead of unicode-range slices.
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
OUT="public/vendor/material-symbols"
ICONS_JSON="public/icons.json"
ICONS_CSS="src/icons.css"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> Reading icons from $ICONS_JSON"
UNICODES="$(python3 - "$ICONS_JSON" <<'EOF'
import json, sys
icons = json.load(open(sys.argv[1]))
print(",".join("U+" + i["codepoint"] for i in icons))
EOF
)"

# The codepoints in icons.css must stay in sync with icons.json; otherwise the
# .ms-* classes would render the wrong (or no) glyph.
echo "==> Verifying $ICONS_CSS matches $ICONS_JSON"
python3 - "$ICONS_JSON" "$ICONS_CSS" <<'EOF'
import json, re, sys
icons = json.load(open(sys.argv[1]))
expected = {".ms-" + i["class"]: i["codepoint"] for i in icons}
css = open(sys.argv[2]).read()
actual = dict(re.findall(r'\.ms-([\w-]+)::before\s*\{\s*content:\s*"\\([0-9a-fA-F]+)";', css))
actual = {".ms-" + k: v for k, v in actual.items()}
if actual != expected:
    print("error: icons.css out of sync with icons.json (re-run after editing either):")
    for k in sorted(set(actual) | set(expected)):
        if actual.get(k) != expected.get(k):
            print(f"  {k}: css={actual.get(k)} json={expected.get(k)}")
    sys.exit(1)
print(f"    {len(expected)} icons, codepoints in sync")
EOF

echo "==> Resolving font URL from Google Fonts CSS"
CSS="$(curl -fsSL -A "$UA" "https://fonts.googleapis.com/css2?family=$FAMILY")"
URL="$(printf '%s' "$CSS" | grep -oP 'url\(\K[^)]+' | head -1)"
[ -n "$URL" ] || { echo "error: could not extract font URL"; exit 1; }
echo "    $URL"

echo "==> Downloading full variable font"
curl -fsSL "$URL" -o "$TMP/full.woff2"

echo "==> Subsetting to $(printf '%s' "$UNICODES" | tr ',' '\n' | grep -c .) icons"
uv run --with fonttools --with brotli pyftsubset \
  "$TMP/full.woff2" \
  --unicodes="$UNICODES" \
  --layout-features='' \
  --flavor=woff2 \
  --output-file="$TMP/subset.woff2"

mkdir -p "$OUT"
mv "$TMP/subset.woff2" "$OUT/material-symbols-outlined.woff2"

echo "==> Fetching Apache-2.0 license text"
curl -fsSL "https://www.apache.org/licenses/LICENSE-2.0.txt" -o "$OUT/LICENSE"

echo "==> Done:"
ls -l "$OUT"
