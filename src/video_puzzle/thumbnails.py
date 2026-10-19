from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

from video_puzzle.i18n import tr

type Runner = Callable[..., CompletedProcess[str]]

# Every scrub spawns one ffmpeg per slot; a hung decode must not outlive the
# worker thread that started it.
THUMBNAIL_TIMEOUT_SECONDS = 20.0


class ThumbnailError(RuntimeError):
    """Failed to extract a still frame from a video file."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


def ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise ThumbnailError(tr("ffmpeg is not installed or not on PATH"))
    return binary


def extract_thumbnail(
    video: Path,
    dest: Path,
    *,
    at_seconds: float = 1.0,
    ffmpeg: str | None = None,
    runner: Runner = subprocess.run,
    timeout: float = THUMBNAIL_TIMEOUT_SECONDS,
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
        try:
            result = runner(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # A hung decode will hang on every other seek position too — fail fast.
            dest.unlink(missing_ok=True)
            raise ThumbnailError(
                tr(
                    "ffmpeg did not respond within {seconds:.0f} s: {video}",
                    seconds=timeout,
                    video=video,
                )
            ) from exc
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise ThumbnailError(
                tr("Could not start ffmpeg for {video}", video=video.name),
                detail=str(exc),
            ) from exc
        if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return dest
        last_error = (result.stderr or result.stdout or "").strip()
        if dest.exists():
            dest.unlink(missing_ok=True)
    raise ThumbnailError(
        tr("Could not extract a frame from {video}", video=video.name),
        detail=last_error,
    )
