from __future__ import annotations

import re


def _int_field(raw: str) -> int | None:
    if raw in {"N/A", "NA"}:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_progress_seconds(line: str) -> float | None:
    """Parse an ffmpeg -progress / stats line into output seconds, if present.

    ffmpeg's ``out_time_ms`` is a deprecated alias of ``out_time_us`` (microseconds).
    """
    stripped = line.strip()
    if stripped.startswith("out_time_us=") or stripped.startswith("out_time_ms="):
        value = _int_field(stripped.split("=", 1)[1])
        if value is None:
            return None
        return value / 1_000_000.0
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
