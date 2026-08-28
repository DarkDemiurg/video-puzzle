from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

type Runner = Callable[..., CompletedProcess[str]]


class ProbeError(RuntimeError):
    """Failed to read media metadata with ffprobe."""


def ffprobe_binary() -> str:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise ProbeError("ffprobe is not installed or not on PATH")
    return binary


@dataclass(frozen=True)
class ProbeResult:
    duration: float
    has_audio: bool


def parse_probe_json(payload: str) -> ProbeResult:
    data = json.loads(payload)
    duration: float | None = None
    has_audio = False
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            has_audio = True
        raw = stream.get("duration")
        if raw is not None:
            try:
                stream_duration = float(raw)
            except (TypeError, ValueError):
                continue
            if duration is None or stream_duration > duration:
                duration = stream_duration
    fmt = data.get("format") or {}
    if fmt.get("duration") is not None:
        try:
            format_duration = float(fmt["duration"])
        except (TypeError, ValueError):
            format_duration = None
        else:
            if duration is None or format_duration > duration:
                duration = format_duration
    if duration is None:
        raise ProbeError("ffprobe did not report a duration")
    return ProbeResult(duration=duration, has_audio=has_audio)


def probe_video(
    video: Path,
    *,
    ffprobe: str | None = None,
    runner: Runner = subprocess.run,
) -> ProbeResult:
    binary = ffprobe or ffprobe_binary()
    cmd = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-show_entries",
        "stream=codec_type,duration",
        "-of",
        "json",
        str(video),
    ]
    result = runner(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ProbeError(detail or f"ffprobe failed for {video}")
    return parse_probe_json(result.stdout or "{}")
