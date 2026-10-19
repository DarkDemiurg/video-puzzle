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

PYTHON_DIST_SHA256="${PYTHON_DIST_SHA256:?set PYTHON_DIST_SHA256 to the archive's sha256}"
PYAPP_REQUIREMENTS="${PYAPP_REQUIREMENTS:?set PYAPP_REQUIREMENTS to a hashed requirements file}"

verify_sha256() {
  local file="$1" expected="$2"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "$expected" "$file" | sha256sum -c -
  else
    printf '%s  %s\n' "$expected" "$file" | shasum -a 256 -c -
  fi
}

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "==> Downloading Python distribution"
curl -fsSL "$PYTHON_DIST_URL" -o "$WORKDIR/python-dist.tar.gz"
verify_sha256 "$WORKDIR/python-dist.tar.gz" "$PYTHON_DIST_SHA256"
tar -xzf "$WORKDIR/python-dist.tar.gz" -C "$WORKDIR"

if [[ -f "$WORKDIR/python/bin/python3" ]]; then
  PYTHON="$WORKDIR/python/bin/python3"
elif [[ -f "$WORKDIR/python/python.exe" ]]; then
  PYTHON="$WORKDIR/python/python.exe"
else
  echo "error: python executable not found in distribution archive" >&2
  exit 1
fi

echo "==> Installing hashed dependencies into embedded Python"
export PIP_DISABLE_PIP_VERSION_CHECK=1
"$PYTHON" -m pip install --no-cache-dir --require-hashes -r "$PYAPP_REQUIREMENTS"
# The wheel is built locally from this checkout; its dependencies are already pinned
# and hash-verified above, so --no-deps keeps pip from resolving them again.
"$PYTHON" -m pip install --no-cache-dir --no-deps "$WHEEL"

echo "==> Verifying import"
"$PYTHON" -c "import video_puzzle; print('ok:', video_puzzle.__file__)"

echo "==> Packing $OUTPUT"
rm -f "$OUTPUT"
tar -czf "$OUTPUT" -C "$WORKDIR" python
echo "==> Done: $OUTPUT"
