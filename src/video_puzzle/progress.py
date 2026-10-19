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
    # Work in whole hundredths: rounding the seconds field at print time let
    # 59.996 render as "0:60.00" instead of rolling over into the next minute.
    hundredths = int(round(max(0.0, seconds) * 100))
    minutes, remainder = divmod(hundredths, 6000)
    hours, minutes = divmod(minutes, 60)
    sec, cents = divmod(remainder, 100)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}.{cents:02d}"
    return f"{minutes}:{sec:02d}.{cents:02d}"


def render_outcome(*, cancelled: bool, code: int, output_exists: bool) -> str:
    """How to treat a finished ffmpeg process: success, cancelled, or failed.

    The exit code alone lies in both directions: ffmpeg may exit 0 without ever
    writing the file, and a cancel that raced a finished render still leaves a
    usable video. Only a zero code *plus* a written file is a success.
    """
    if code == 0 and output_exists:
        return "success"
    if cancelled:
        return "cancelled"
    return "failed"


def should_delete_partial_output(*, outcome: str, preexisting: bool) -> bool:
    """Whether a run that did not produce a usable video left junk to clean up.

    A file that already existed before the run is the user's own data: ffmpeg was
    told to overwrite it, so deleting it too would destroy something we never made.
    """
    return outcome in {"cancelled", "failed"} and not preexisting
