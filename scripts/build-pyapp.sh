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

VERSION="$(grep -E '^version = ' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')"

echo "==> Building wheel (video-puzzle $VERSION)"
uv build --wheel
WHEEL="$(ls -1 dist/video_puzzle-"${VERSION}"-*.whl | head -1)"

if [[ ! -d "$PYAPP_DIR" ]]; then
  echo "==> Fetching PyApp $PYAPP_VERSION"
  mkdir -p "$(dirname "$PYAPP_DIR")"
  curl -fsSL "https://github.com/ofek/pyapp/releases/download/v${PYAPP_VERSION}/source.tar.gz" \
    | tar -xz -C "$(dirname "$PYAPP_DIR")"
fi

echo "==> Compiling PyApp binary"
export PYAPP_PROJECT_NAME=video-puzzle
export PYAPP_PROJECT_VERSION="$VERSION"
export PYAPP_PROJECT_PATH="$WHEEL"
export PYAPP_PYTHON_VERSION=3.12
export PYAPP_EXEC_MODULE=video_puzzle
export PYAPP_IS_GUI=1
export PYAPP_DISTRIBUTION_EMBED=1

(
  cd "$PYAPP_DIR"
  cargo build --release
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
