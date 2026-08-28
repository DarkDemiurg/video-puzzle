from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

import numpy as np

from video_puzzle.state import AppState, Slot

type BytesRunner = Callable[..., CompletedProcess[bytes]]

AUDIO_RATE = 8000
ANALYZE_SECONDS = 45.0
MAX_LAG_SECONDS = 12.0
MIN_SCORE = 0.08


class SyncError(RuntimeError):
    """Could not estimate an audio alignment."""


def ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SyncError("ffmpeg is not installed or not on PATH")
    return binary


def extract_mono_pcm(
    video: Path,
    *,
    rate: int = AUDIO_RATE,
    max_seconds: float = ANALYZE_SECONDS,
    ffmpeg: str | None = None,
    runner: BytesRunner = subprocess.run,
) -> np.ndarray:
    binary = ffmpeg or ffmpeg_binary()
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-t",
        str(max_seconds),
        "-ac",
        "1",
        "-ar",
        str(rate),
        "-f",
        "f32le",
        "pipe:1",
    ]
    result = runner(cmd, capture_output=True, check=False)
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise SyncError(detail or f"Could not extract audio from {video}")
    pcm = np.frombuffer(result.stdout, dtype=np.float32)
    if pcm.size == 0:
        raise SyncError(f"Empty audio stream in {video}")
    return pcm.astype(np.float64, copy=False)


def lag_seconds(
    reference: np.ndarray,
    other: np.ndarray,
    sample_rate: int,
    *,
    max_lag_s: float = MAX_LAG_SECONDS,
) -> tuple[float, float]:
    """Return (lag, score). Positive lag means `other` starts later than `reference`."""
    ref = np.asarray(reference, dtype=np.float64)
    sig = np.asarray(other, dtype=np.float64)
    if ref.size < sample_rate // 4 or sig.size < sample_rate // 4:
        raise SyncError("Audio is too short to correlate")
    ref = ref - ref.mean()
    sig = sig - sig.mean()
    n = 1 << int(np.ceil(np.log2(ref.size + sig.size - 1)))
    corr = np.fft.irfft(np.fft.rfft(ref, n) * np.conj(np.fft.rfft(sig, n)), n)
    max_lag = min(int(max_lag_s * sample_rate), ref.size - 1, sig.size - 1)
    if max_lag < 1:
        return 0.0, 0.0
    lags = np.arange(-max_lag, max_lag + 1)
    values = np.empty(lags.shape, dtype=np.float64)
    values[:max_lag] = corr[-max_lag:]
    values[max_lag:] = corr[: max_lag + 1]
    peak_index = int(np.argmax(values))
    denom = float(np.linalg.norm(ref) * np.linalg.norm(sig))
    score = float(values[peak_index] / denom) if denom else 0.0
    return float(lags[peak_index]) / sample_rate, score


def trims_from_lags(lags: Sequence[float | None]) -> list[float]:
    """Convert delays-vs-reference into start trims so the latest start becomes t=0."""
    values = [0.0 if lag is None else lag for lag in lags]
    latest = max(values, default=0.0)
    return [latest - value for value in values]


@dataclass(frozen=True)
class ClipAlignment:
    index: int
    trim_start: float
    missing_tail: float
    lag: float | None
    score: float | None


@dataclass(frozen=True)
class SyncResult:
    trims: list[float]
    alignments: list[ClipAlignment]
    overlap: float
    used_audio: bool
    summary: str


def _duration_only_sync(slots: Sequence[Slot]) -> SyncResult:
    durations = [slot.duration or 0.0 for slot in slots]
    overlap = min(durations) if durations else 0.0
    alignments = [
        ClipAlignment(
            index=index,
            trim_start=0.0,
            missing_tail=max(0.0, (slot.duration or 0.0) - overlap),
            lag=None,
            score=None,
        )
        for index, slot in enumerate(slots)
    ]
    spread = max(durations) - overlap if durations else 0.0
    if spread < 0.05:
        summary = "Длительности почти равны; сдвиг не виден."
    else:
        summary = (
            "Звука недостаточно для синхронизации. Считаем, что ролики начинаются вместе; "
            f"разница {spread:.2f} с — в хвосте."
        )
    return SyncResult(
        trims=[0.0] * len(slots),
        alignments=alignments,
        overlap=overlap,
        used_audio=False,
        summary=summary,
    )


def align_slots(
    slots: Sequence[Slot],
    *,
    pcm: Sequence[np.ndarray | None],
    sample_rate: int = AUDIO_RATE,
    max_lag_s: float = MAX_LAG_SECONDS,
    min_score: float = MIN_SCORE,
) -> SyncResult:
    if len(pcm) != len(slots):
        raise ValueError("pcm list must match slot count")
    audio_indexes = [i for i, samples in enumerate(pcm) if samples is not None]
    if len(audio_indexes) < 2:
        return _duration_only_sync(slots)

    reference = audio_indexes[0]
    lags: list[float | None] = [None] * len(slots)
    scores: list[float | None] = [None] * len(slots)
    lags[reference] = 0.0
    scores[reference] = 1.0
    weak: list[int] = []
    for index in audio_indexes[1:]:
        lag, score = lag_seconds(pcm[reference], pcm[index], sample_rate, max_lag_s=max_lag_s)
        if score < min_score:
            lags[index] = 0.0
            scores[index] = score
            weak.append(index)
        else:
            lags[index] = lag
            scores[index] = score
    for index, samples in enumerate(pcm):
        if samples is None:
            lags[index] = 0.0

    trims = trims_from_lags(lags)
    effective = [
        max(0.0, (slot.duration or 0.0) - trim) for slot, trim in zip(slots, trims, strict=True)
    ]
    overlap = min(effective) if effective else 0.0
    alignments = [
        ClipAlignment(
            index=index,
            trim_start=trim,
            missing_tail=max(0.0, (slot.duration or 0.0) - trim - overlap),
            lag=lags[index],
            score=scores[index],
        )
        for index, (slot, trim) in enumerate(zip(slots, trims, strict=True))
    ]
    summary = _summarize(alignments, used_audio=True, weak=weak)
    return SyncResult(
        trims=trims,
        alignments=alignments,
        overlap=overlap,
        used_audio=True,
        summary=summary,
    )


def _summarize(
    alignments: Sequence[ClipAlignment], *, used_audio: bool, weak: Sequence[int]
) -> str:
    parts: list[str] = []
    if used_audio:
        parts.append("Синхронизация по звуку.")
    for item in alignments:
        bits: list[str] = []
        if item.trim_start >= 0.05:
            bits.append(f"лишние {item.trim_start:.2f} с в начале")
        if item.missing_tail >= 0.05:
            bits.append(f"нет {item.missing_tail:.2f} с в конце")
        if not bits:
            bits.append("совпадает с общим отрезком")
        parts.append(f"Клип {item.index + 1}: {', '.join(bits)}")
    if weak:
        labels = ", ".join(str(i + 1) for i in weak)
        parts.append(f"Слабая корреляция у клипа(ов) {labels} — сдвиг принят за 0.")
    return " ".join(parts)


def analyze_slots(slots: Sequence[Slot], *, ffmpeg: str | None = None) -> SyncResult:
    samples: list[np.ndarray | None] = []
    for slot in slots:
        if slot.path is None or not slot.has_audio:
            samples.append(None)
            continue
        try:
            samples.append(extract_mono_pcm(slot.path, ffmpeg=ffmpeg))
        except SyncError:
            samples.append(None)
    return align_slots(slots, pcm=samples)


def apply_sync(state: AppState, result: SyncResult) -> None:
    for index, trim in enumerate(result.trims):
        if index >= state.active_count:
            break
        state.slots[index].trim_start = max(0.0, trim)
    state.clamp_playhead()
