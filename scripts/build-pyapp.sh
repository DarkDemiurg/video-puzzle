#!/usr/bin/env bash
# Build a standalone Video Puzzle binary with PyApp (https://ofek.dev/pyapp/).
# Requires: Rust (cargo), curl, tar. ffmpeg/ffprobe are still required at runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYAPP_VERSION="${PYAPP_VERSION:-0.29.0}"
PYAPP_DIR="${PYAPP_DIR:-$ROOT/.pyapp/pyapp-v${PYAPP_VERSION}}"
OUTPUT_NAME="${OUTPUT_NAME:-video-puzzle}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/dist/pyapp}"

# Pinned PyApp source archive (v0.29.0). Override both URL and hash when bumping.
PYAPP_SHA256="${PYAPP_SHA256:-0533004baf6d1d46ef15abad02e98881ec92291c024182a36b57931c8d66df5f}"

# python-build-standalone URL used by PyApp 0.29.0 for CPython 3.12 (override per platform).
DEFAULT_PYTHON_DIST_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
DEFAULT_PYTHON_DIST_SHA256="c74addcd1b033a6e4d60ead3ab47fcc995569027e01d3061c4a934f363c4a0cf"
PYAPP_PYTHON_DIST_URL="${PYAPP_PYTHON_DIST_URL:-$DEFAULT_PYTHON_DIST_URL}"
if [[ -z "${PYAPP_PYTHON_DIST_SHA256:-}" ]]; then
  if [[ "$PYAPP_PYTHON_DIST_URL" == "$DEFAULT_PYTHON_DIST_URL" ]]; then
    PYAPP_PYTHON_DIST_SHA256="$DEFAULT_PYTHON_DIST_SHA256"
  else
    echo "error: set PYAPP_PYTHON_DIST_SHA256 when overriding PYAPP_PYTHON_DIST_URL" >&2
    exit 1
  fi
fi
PYAPP_PYTHON_PATH="${PYAPP_PYTHON_PATH:-python/bin/python3}"

verify_sha256() {
  local file="$1" expected="$2"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "$expected" "$file" | sha256sum -c -
  else
    printf '%s  %s\n' "$expected" "$file" | shasum -a 256 -c -
  fi
}

VERSION="$(grep -E '^version = ' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')"

echo "==> Building wheel (video-puzzle $VERSION)"
uv build --wheel
WHEEL="$(ls -1 dist/video_puzzle-"${VERSION}"-*.whl | head -1)"

if [[ ! -d "$PYAPP_DIR" ]]; then
  echo "==> Fetching PyApp $PYAPP_VERSION"
  mkdir -p "$(dirname "$PYAPP_DIR")"
  PYAPP_ARCHIVE="$(mktemp -t pyapp-source.XXXXXX.tar.gz)"
  curl -fsSL "https://github.com/ofek/pyapp/releases/download/v${PYAPP_VERSION}/source.tar.gz" \
    -o "$PYAPP_ARCHIVE"
  verify_sha256 "$PYAPP_ARCHIVE" "$PYAPP_SHA256"
  tar -xzf "$PYAPP_ARCHIVE" -C "$(dirname "$PYAPP_DIR")"
  rm -f "$PYAPP_ARCHIVE"
fi

DIST_ARCHIVE="$ROOT/.pyapp/python-distribution.tar.gz"
REQUIREMENTS="$ROOT/.pyapp/pyapp-requirements.txt"
mkdir -p "$ROOT/.pyapp"
echo "==> Exporting hashed runtime requirements"
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o "$REQUIREMENTS"
echo "==> Preparing embedded Python distribution"
chmod +x scripts/prepare-pyapp-distribution.sh
PYTHON_DIST_SHA256="$PYAPP_PYTHON_DIST_SHA256" \
PYAPP_REQUIREMENTS="$REQUIREMENTS" \
  scripts/prepare-pyapp-distribution.sh "$WHEEL" "$PYAPP_PYTHON_DIST_URL" "$DIST_ARCHIVE"

echo "==> Compiling PyApp binary"
export PYAPP_PROJECT_NAME=video-puzzle
export PYAPP_PROJECT_VERSION="$VERSION"
export PYAPP_PYTHON_VERSION=3.12
export PYAPP_EXEC_MODULE=video_puzzle
export PYAPP_IS_GUI=1
export PYAPP_DISTRIBUTION_PATH="$DIST_ARCHIVE"
export PYAPP_DISTRIBUTION_PYTHON_PATH="$PYAPP_PYTHON_PATH"
export PYAPP_SKIP_INSTALL=1
export PYAPP_FULL_ISOLATION=1

(
  cd "$PYAPP_DIR"
  cargo build --release --locked
)

mkdir -p "$OUTPUT_DIR"
BIN="$PYAPP_DIR/target/release/pyapp"
if [[ -f "${BIN}.exe" ]]; then
  BIN="${BIN}.exe"
  OUT="$OUTPUT_DIR/${OUTPUT_NAME}.exe"
else
  OUT="$OUTPUT_DIR/$OUTPUT_NAME"
fi

cp "$BIN" "$OUT"
chmod +x "$OUT"

echo "==> Done: $OUT"
echo "    ffmpeg and ffprobe must be available on PATH when running the app."
