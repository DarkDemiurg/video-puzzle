from __future__ import annotations

import re

PROGRESS_TIME_RE = re.compile(
    r"(?:out_time_ms=(\d+)|out_time_us=(\d+)|out_time=(\d+):(\d+):(\d+(?:\.\d+)?)|time=(\d+):(\d+):(\d+(?:\.\d+)?))"
)


def parse_progress_seconds(line: str) -> float | None:
    """Parse an ffmpeg -progress / stats line into seconds, if present."""
    stripped = line.strip()
    if stripped.startswith("out_time_ms="):
        value = stripped.split("=", 1)[1]
        if value in {"N/A", "NA"}:
            return None
        return int(value) / 1000.0
    if stripped.startswith("out_time_us="):
        value = stripped.split("=", 1)[1]
        if value in {"N/A", "NA"}:
            return None
        return int(value) / 1_000_000.0
    match = re.search(r"(?:out_time|time)=(\d+):(\d+):(\d+(?:\.\d+)?)", stripped)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def progress_fraction(current: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, current / total))


def format_timecode(seconds: float) -> str:
    value = max(0.0, seconds)
    minutes, sec = divmod(value, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:05.2f}"
    return f"{int(minutes)}:{sec:05.2f}"


def render_outcome(*, cancelled: bool, code: int) -> str:
    """How to treat a finished ffmpeg process: success, cancelled, or failed."""
    if code == 0:
        return "success"
    if cancelled:
        return "cancelled"
    return "failed"


def should_delete_partial_output(*, cancelled: bool, code: int) -> bool:
    return cancelled and code != 0
