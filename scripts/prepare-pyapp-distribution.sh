#!/usr/bin/env bash
# Prepare an embedded Python distribution with the app and all dependencies preinstalled.
# Used by PyApp with PYAPP_SKIP_INSTALL=1 for offline, fast startup.
set -euo pipefail

WHEEL="${1:?usage: prepare-pyapp-distribution.sh <wheel> <python-dist-url> [output.tar.gz]}"
PYTHON_DIST_URL="${2:?usage: prepare-pyapp-distribution.sh <wheel> <python-dist-url> [output.tar.gz]}"
OUTPUT="${3:-pyapp-distribution.tar.gz}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEEL="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"
if [[ "$OUTPUT" != /* ]]; then
  OUTPUT="$(pwd)/$OUTPUT"
fi

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "==> Downloading Python distribution"
curl -fsSL "$PYTHON_DIST_URL" -o "$WORKDIR/python-dist.tar.gz"
tar -xzf "$WORKDIR/python-dist.tar.gz" -C "$WORKDIR"

if [[ -f "$WORKDIR/python/bin/python3" ]]; then
  PYTHON="$WORKDIR/python/bin/python3"
elif [[ -f "$WORKDIR/python/python.exe" ]]; then
  PYTHON="$WORKDIR/python/python.exe"
else
  echo "error: python executable not found in distribution archive" >&2
  exit 1
fi

echo "==> Installing $WHEEL and dependencies into embedded Python"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install "$WHEEL"

echo "==> Verifying import"
"$PYTHON" -c "import video_puzzle; print('ok:', video_puzzle.__file__)"

echo "==> Packing $OUTPUT"
rm -f "$OUTPUT"
tar -czf "$OUTPUT" -C "$WORKDIR" python
echo "==> Done: $OUTPUT"
