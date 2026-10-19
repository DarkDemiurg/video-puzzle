from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

from video_puzzle.i18n import tr

type Runner = Callable[..., CompletedProcess[str]]

# ffprobe reads only the header; a longer wait means the file or mount is stuck.
PROBE_TIMEOUT_SECONDS = 15.0


class ProbeError(RuntimeError):
    """Failed to read media metadata with ffprobe."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


def ffprobe_binary() -> str:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise ProbeError(tr("ffprobe is not installed or not on PATH"))
    return binary


@dataclass(frozen=True)
class ProbeResult:
    duration: float
    has_audio: bool
    fps: float | None = None
    has_video: bool = True


def parse_frame_rate(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text in {"0/0", "0"}:
        return None
    try:
        if "/" in text:
            num_s, den_s = text.split("/", 1)
            num, den = float(num_s), float(den_s)
            if den == 0:
                return None
            value = num / den
        else:
            value = float(text)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 240:
        return None
    return value


def parse_probe_json(payload: str) -> ProbeResult:
    """Parse ffprobe output, rejecting malformed JSON instead of leaking exceptions.

    Video duration wins over ``format.duration``: a container often reports the
    longest stream (for example a 12 s audio track next to a 10 s video), and using
    that value stretched the timeline, trims and the progress bar past the real clip.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProbeError(tr("Invalid ffprobe response"), detail=str(exc)) from exc
    if not isinstance(data, dict):
        raise ProbeError(tr("Invalid ffprobe response: expected a JSON object"))
    streams = data.get("streams") or []
    if not isinstance(streams, list):
        raise ProbeError(tr("Invalid ffprobe response: streams is not a list"))
    video_durations: list[float] = []
    format_duration: float | None = None
    has_audio = False
    has_video = False
    fps: float | None = None
    for stream in streams:
        if not isinstance(stream, dict):
            raise ProbeError(tr("Invalid ffprobe response: stream is not an object"))
        if stream.get("codec_type") == "audio":
            has_audio = True
            continue
        if stream.get("codec_type") != "video":
            continue
        has_video = True
        if fps is None:
            fps = parse_frame_rate(stream.get("avg_frame_rate")) or parse_frame_rate(
                stream.get("r_frame_rate")
            )
        raw = stream.get("duration")
        if raw is None:
            continue
        try:
            stream_duration = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(stream_duration) and stream_duration > 0:
            video_durations.append(stream_duration)
    fmt = data.get("format")
    if fmt is not None and not isinstance(fmt, dict):
        raise ProbeError(tr("Invalid ffprobe response: format is not an object"))
    if isinstance(fmt, dict) and fmt.get("duration") is not None:
        try:
            format_duration = float(fmt["duration"])
        except (TypeError, ValueError):
            format_duration = None
        if format_duration is not None and (
            not math.isfinite(format_duration) or format_duration <= 0
        ):
            format_duration = None
    duration = max(video_durations) if video_durations else format_duration
    if duration is None:
        raise ProbeError(tr("ffprobe did not report a duration"))
    return ProbeResult(duration=duration, has_audio=has_audio, fps=fps, has_video=has_video)


def probe_video(
    video: Path,
    *,
    ffprobe: str | None = None,
    runner: Runner = subprocess.run,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    binary = ffprobe or ffprobe_binary()
    cmd = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-show_entries",
        "stream=codec_type,duration,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        str(video),
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(
            tr(
                "ffprobe did not respond within {seconds:.0f} s: {video}",
                seconds=timeout,
                video=video,
            )
        ) from exc
    except OSError as exc:
        raise ProbeError(
            tr("Could not start ffprobe for {video}", video=video.name),
            detail=str(exc),
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ProbeError(
            tr("Could not read video metadata for {video}", video=video.name),
            detail=detail,
        )
    return parse_probe_json(result.stdout or "{}")
