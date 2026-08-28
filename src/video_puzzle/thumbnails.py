from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

type Runner = Callable[..., CompletedProcess[str]]


class ThumbnailError(RuntimeError):
    """Failed to extract a still frame from a video file."""


def ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise ThumbnailError("ffmpeg is not installed or not on PATH")
    return binary


def extract_thumbnail(
    video: Path,
    dest: Path,
    *,
    at_seconds: float = 1.0,
    ffmpeg: str | None = None,
    runner: Runner = subprocess.run,
) -> Path:
    """Write a JPEG still from `video` to `dest` at `at_seconds`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    binary = ffmpeg or ffmpeg_binary()
    seeks = [max(0.0, at_seconds)]
    if at_seconds > 0.08:
        seeks.append(max(0.0, at_seconds - 0.08))
    if 0.0 < at_seconds <= 2.0 and 0.0 not in seeks:
        seeks.append(0.0)
    last_error = ""
    for seek in seeks:
        cmd = [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(seek),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dest),
        ]
        result = runner(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return dest
        last_error = (result.stderr or result.stdout or "").strip()
        if dest.exists():
            dest.unlink(missing_ok=True)
    raise ThumbnailError(last_error or f"Could not extract a frame from {video}")
