from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from subprocess import CompletedProcess

from video_puzzle.encode import EncoderKind

type Runner = Callable[..., CompletedProcess[str]]

HW_ENCODERS = ("h264_nvenc", "h264_qsv")

_PROBE_PIXFMT = {
    "h264_nvenc": "yuv420p",
    "h264_qsv": "nv12",
}


def ffmpeg_binary() -> str | None:
    return shutil.which("ffmpeg")


def parse_encoder_list(payload: str) -> set[str]:
    names: set[str] = set()
    for line in payload.splitlines():
        line = line.strip()
        if "h264_nvenc" in line:
            names.add("h264_nvenc")
        if "h264_qsv" in line:
            names.add("h264_qsv")
        if "libx264" in line:
            names.add("libx264")
    return names


def probe_encoder(name: str, *, ffmpeg: str, runner: Runner = subprocess.run) -> bool:
    """True if ffmpeg can actually open this video encoder (not just list it)."""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:r=25:d=0.08",
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        name,
        "-pix_fmt",
        _PROBE_PIXFMT.get(name, "yuv420p"),
        "-f",
        "null",
        "-",
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, check=False, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_encoders(*, ffmpeg: str | None = None, runner: Runner = subprocess.run) -> set[str]:
    binary = ffmpeg or ffmpeg_binary()
    if binary is None:
        return {"libx264"}
    result = runner(
        [binary, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    names = parse_encoder_list((result.stdout or "") + (result.stderr or ""))
    names.add("libx264")
    working = {"libx264"}
    for name in HW_ENCODERS:
        if name in names and probe_encoder(name, ffmpeg=binary, runner=runner):
            working.add(name)
    return working


def resolve_encoder(kind: EncoderKind, available: set[str]) -> EncoderKind:
    if kind is EncoderKind.AUTO:
        if "h264_nvenc" in available:
            return EncoderKind.NVENC
        if "h264_qsv" in available:
            return EncoderKind.QSV
        return EncoderKind.CPU
    if kind is EncoderKind.NVENC and "h264_nvenc" not in available:
        return EncoderKind.CPU
    if kind is EncoderKind.QSV and "h264_qsv" not in available:
        return EncoderKind.CPU
    return kind
