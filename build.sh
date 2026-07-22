#!/usr/bin/env bash
# Build the Trilobite pip package: frontend first, then the Python wheel/sdist.
set -euo pipefail

# Always run from the repo root, no matter where the script is invoked from.
cd "$(dirname "$0")"

echo "==> Building frontend"
(
  cd frontend
  # `npm ci` requires package-lock.json and gives a clean, reproducible install.
  npm ci
  npm run build
)

echo "==> Building Python package (sdist + wheel)"
# uv build builds the sdist first, then the wheel from the sdist, so MANIFEST.in
# controls which data files (frontend assets, config_example) end up inside.
uv build

echo "==> Done. Artifacts in dist/:"
ls -1 dist/
