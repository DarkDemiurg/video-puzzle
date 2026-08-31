from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EncodeQuality(StrEnum):
    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"
    ORIGINAL = "original"


class EncoderKind(StrEnum):
    AUTO = "auto"
    CPU = "libx264"
    NVENC = "h264_nvenc"
    QSV = "h264_qsv"


@dataclass(frozen=True)
class EncodeSettings:
    crf: int
    preset: str
    audio_bitrate: str
    nvenc_cq: int
    qsv_quality: int


ENCODE_PRESETS: dict[EncodeQuality, EncodeSettings] = {
    EncodeQuality.DRAFT: EncodeSettings(23, "veryfast", "128k", nvenc_cq=28, qsv_quality=28),
    EncodeQuality.STANDARD: EncodeSettings(18, "medium", "192k", nvenc_cq=23, qsv_quality=22),
    EncodeQuality.HIGH: EncodeSettings(15, "slow", "256k", nvenc_cq=19, qsv_quality=18),
    EncodeQuality.ORIGINAL: EncodeSettings(12, "slow", "320k", nvenc_cq=15, qsv_quality=14),
}

DEFAULT_QUALITY = EncodeQuality.STANDARD
DEFAULT_ENCODER = EncoderKind.AUTO

_BPP = {23: 0.045, 18: 0.08, 15: 0.12, 12: 0.2}


def estimate_output_bytes(
    *,
    width: int,
    height: int,
    fps: float,
    duration: float,
    quality: EncodeQuality,
    has_audio: bool,
) -> int:
    settings = ENCODE_PRESETS[quality]
    bpp = _BPP.get(settings.crf, 0.08)
    video_bits = width * height * max(1.0, fps) * max(0.0, duration) * bpp
    audio_bits = 0.0
    if has_audio:
        rate = int(settings.audio_bitrate.rstrip("k")) * 1000
        audio_bits = rate * max(0.0, duration)
    return int((video_bits + audio_bits) / 8)


def format_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} Б"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f} КБ"
    if nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.0f} МБ"
    return f"{nbytes / (1024 * 1024 * 1024):.1f} ГБ"


def video_encoder_args(kind: EncoderKind, quality: EncodeQuality) -> list[str]:
    settings = ENCODE_PRESETS[quality]
    if kind is EncoderKind.NVENC:
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4" if quality is not EncodeQuality.ORIGINAL else "p6",
            "-rc",
            "vbr",
            "-cq",
            str(settings.nvenc_cq),
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    if kind is EncoderKind.QSV:
        return [
            "-c:v",
            "h264_qsv",
            "-global_quality",
            str(settings.qsv_quality),
            "-look_ahead",
            "1",
            "-pix_fmt",
            "nv12",
        ]
    return [
        "-c:v",
        "libx264",
        "-crf",
        str(settings.crf),
        "-preset",
        settings.preset,
        "-pix_fmt",
        "yuv420p",
    ]
