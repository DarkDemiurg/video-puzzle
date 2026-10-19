from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median
from subprocess import CompletedProcess

import numpy as np

from video_puzzle.i18n import tr
from video_puzzle.state import AppState, Slot

type BytesRunner = Callable[..., CompletedProcess[bytes]]

AUDIO_RATE = 8000
ANALYZE_SECONDS = 45.0
MAX_LAG_SECONDS = 12.0
# Correlation of the overlapping audio: ~1 for the same moment, ~0 for unrelated clips.
# 0.3 let periodic music and shared rhythms align on the wrong beat.
MIN_SCORE = 0.5
# Peak / runner-up ratio outside ±0.25 s. A true match has one dominant peak; a tone or
# a common beat produces several comparable ones, so its "score" must not be trusted.
MIN_CONFIDENCE = 2.0
CONFIDENCE_GUARD_SECONDS = 0.25
REFERENCE_LIMIT = 3
# Decoding 45 s of mono 8 kHz audio is fast; anything slower means ffmpeg is stuck.
AUDIO_EXTRACT_TIMEOUT_SECONDS = 60.0


class SyncError(RuntimeError):
    """Could not estimate an audio alignment."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


def ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SyncError(tr("ffmpeg is not installed or not on PATH"))
    return binary


def extract_mono_pcm(
    video: Path,
    *,
    rate: int = AUDIO_RATE,
    max_seconds: float = ANALYZE_SECONDS,
    ffmpeg: str | None = None,
    runner: BytesRunner = subprocess.run,
    timeout: float = AUDIO_EXTRACT_TIMEOUT_SECONDS,
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
    try:
        result = runner(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SyncError(tr("Reading audio from {name} took too long", name=video.name)) from exc
    except OSError as exc:
        raise SyncError(
            tr("Could not start ffmpeg for {name}", name=video.name),
            detail=str(exc),
        ) from exc
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise SyncError(
            tr("Could not extract audio from {video}", video=video.name),
            detail=detail,
        )
    pcm = np.frombuffer(result.stdout, dtype=np.float32)
    if pcm.size == 0:
        raise SyncError(tr("Empty audio stream in {video}", video=video))
    return pcm.astype(np.float64, copy=False)


def _lag_scores(
    reference: np.ndarray,
    other: np.ndarray,
    sample_rate: int,
    *,
    max_lag_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    ref = np.asarray(reference, dtype=np.float64)
    sig = np.asarray(other, dtype=np.float64)
    if ref.size < sample_rate // 4 or sig.size < sample_rate // 4:
        raise SyncError(tr("Audio is too short to correlate"))
    ref = ref - ref.mean()
    sig = sig - sig.mean()
    n = 1 << int(np.ceil(np.log2(ref.size + sig.size - 1)))
    corr = np.fft.irfft(np.fft.rfft(ref, n) * np.conj(np.fft.rfft(sig, n)), n)
    max_lag = min(int(max_lag_s * sample_rate), ref.size - 1, sig.size - 1)
    if max_lag < 1:
        return np.zeros(1), np.zeros(1)
    lags = np.arange(-max_lag, max_lag + 1)
    values = np.empty(lags.shape, dtype=np.float64)
    values[:max_lag] = corr[-max_lag:]
    values[max_lag:] = corr[: max_lag + 1]
    denom = float(np.linalg.norm(ref) * np.linalg.norm(sig))
    scores = values / denom if denom else np.zeros_like(values)
    return lags, scores


def _peak_confidence(scores: np.ndarray, lags: np.ndarray, sample_rate: int) -> float:
    """How much the best lag stands out from every peak outside its neighbourhood.

    Identical noise has one sharp peak and a huge ratio; a periodic signal repeats its
    peak at the period, so the runner-up is nearly as strong and confidence stays low.
    """
    peak_index = int(np.argmax(scores))
    peak = abs(float(scores[peak_index]))
    guard = int(CONFIDENCE_GUARD_SECONDS * sample_rate)
    mask = np.abs(lags - lags[peak_index]) > guard
    if not mask.any():
        return float("inf")
    runner_up = float(np.max(np.abs(scores[mask])))
    if runner_up <= 1e-9:
        return float("inf")
    return peak / runner_up


def lag_seconds_with_confidence(
    reference: np.ndarray,
    other: np.ndarray,
    sample_rate: int,
    *,
    max_lag_s: float = MAX_LAG_SECONDS,
) -> tuple[float, float, float]:
    """Return (lag, score, confidence). Positive lag means `other` starts later."""
    lags, scores = _lag_scores(reference, other, sample_rate, max_lag_s=max_lag_s)
    peak_index = int(np.argmax(scores))
    return (
        float(lags[peak_index]) / sample_rate,
        float(scores[peak_index]),
        _peak_confidence(scores, lags, sample_rate),
    )


def lag_seconds(
    reference: np.ndarray,
    other: np.ndarray,
    sample_rate: int,
    *,
    max_lag_s: float = MAX_LAG_SECONDS,
) -> tuple[float, float]:
    """Return (lag, score). Positive lag means `other` starts later than `reference`."""
    lag, score, _confidence = lag_seconds_with_confidence(
        reference, other, sample_rate, max_lag_s=max_lag_s
    )
    return lag, score


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
    audio_errors: tuple[int, ...] = ()


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
        summary = tr("Durations are almost equal; no visible offset.")
    else:
        summary = tr(
            "Not enough audio to synchronize. Assuming the clips start together; "
            "the {seconds:.2f} s difference is at the end.",
            seconds=spread,
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
    min_confidence: float = MIN_CONFIDENCE,
) -> SyncResult:
    if len(pcm) != len(slots):
        raise ValueError(tr("pcm list must match slot count"))
    audio_indexes = [i for i, samples in enumerate(pcm) if samples is not None]
    if len(audio_indexes) < 2:
        return _duration_only_sync(slots)

    # Anchoring every clip to the first one made a single bad reference corrupt all
    # offsets. A few references (first/middle/last) are compared pairwise and the median
    # absolute lag is used instead.
    reference = audio_indexes[0]
    references = [reference]
    for candidate in (audio_indexes[len(audio_indexes) // 2], audio_indexes[-1]):
        if candidate not in references:
            references.append(candidate)
    references = references[:REFERENCE_LIMIT]

    pair_cache: dict[tuple[int, int], tuple[float, float, float] | None] = {}

    def pair(first: int, second: int) -> tuple[float, float, float] | None:
        key = (first, second)
        if key not in pair_cache:
            first_pcm = pcm[first]
            second_pcm = pcm[second]
            assert first_pcm is not None and second_pcm is not None
            try:
                pair_cache[key] = lag_seconds_with_confidence(
                    first_pcm, second_pcm, sample_rate, max_lag_s=max_lag_s
                )
            except SyncError:
                pair_cache[key] = None
        return pair_cache[key]

    base_lags: dict[int, float | None] = {reference: 0.0}
    for candidate in references[1:]:
        measured = pair(reference, candidate)
        if measured is None:
            base_lags[candidate] = None
            continue
        lag, score, confidence = measured
        base_lags[candidate] = lag if score >= min_score and confidence >= min_confidence else None

    lags: list[float | None] = [None] * len(slots)
    scores: list[float | None] = [None] * len(slots)
    lags[reference] = 0.0
    scores[reference] = 1.0
    weak: list[int] = []
    for index in audio_indexes:
        if index == reference:
            continue
        candidates: list[tuple[float, float]] = []
        best_weak = 0.0
        for anchor in references:
            if anchor == index:
                continue
            base = base_lags.get(anchor)
            if base is None:
                continue
            measured = pair(anchor, index)
            if measured is None:
                continue
            lag, score, confidence = measured
            best_weak = max(best_weak, score)
            if score >= min_score and confidence >= min_confidence:
                candidates.append((base + lag, score))
        if candidates:
            lags[index] = median(item[0] for item in candidates)
            scores[index] = median(item[1] for item in candidates)
        else:
            # No trustworthy anchor: assume shared start, but report the weak match.
            lags[index] = 0.0
            scores[index] = best_weak
            weak.append(index)
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
        parts.append(tr("Synchronized by audio."))
    for item in alignments:
        bits: list[str] = []
        if item.trim_start >= 0.05:
            bits.append(tr("extra {seconds:.2f} s at the start", seconds=item.trim_start))
        if item.missing_tail >= 0.05:
            bits.append(tr("missing {seconds:.2f} s at the end", seconds=item.missing_tail))
        if not bits:
            bits.append(tr("matches the common segment"))
        parts.append(
            tr(
                "Clip {slot}: {details}",
                slot=item.index + 1,
                details=", ".join(bits),
            )
        )
    if weak:
        labels = ", ".join(str(i + 1) for i in weak)
        parts.append(
            tr(
                "Weak correlation for clip(s) {slots} — offset assumed to be 0.",
                slots=labels,
            )
        )
    return " ".join(parts)


def analyze_slots(
    slots: Sequence[Slot],
    *,
    ffmpeg: str | None = None,
    runner: BytesRunner | None = None,
    timeout: float = AUDIO_EXTRACT_TIMEOUT_SECONDS,
) -> SyncResult:
    samples: list[np.ndarray | None] = []
    failed: list[int] = []
    for index, slot in enumerate(slots):
        if slot.path is None or not slot.has_audio:
            samples.append(None)
            continue
        try:
            if runner is None:
                samples.append(extract_mono_pcm(slot.path, ffmpeg=ffmpeg, timeout=timeout))
            else:
                samples.append(
                    extract_mono_pcm(slot.path, ffmpeg=ffmpeg, timeout=timeout, runner=runner)
                )
        except SyncError:
            samples.append(None)
            failed.append(index)
    result = align_slots(slots, pcm=samples)
    if failed:
        labels = ", ".join(str(index + 1) for index in failed)
        return replace(
            result,
            summary=tr(
                "{summary} Audio could not be read: clip(s) {slots}.",
                summary=result.summary,
                slots=labels,
            ),
            audio_errors=tuple(failed),
        )
    return result


def apply_sync(state: AppState, result: SyncResult) -> None:
    """Move every clip so the shared audio moment lands on the start of the output.

    In puzzle mode the offset is a start trim. A wall ignores ``trim_start`` entirely
    — its window comes from the I/O marks — so writing trims there silently did
    nothing: the summary and the thumbnails changed while the ffmpeg command stayed
    the same. The wall therefore shifts each fragment by the same amount.
    """
    for index, trim in enumerate(result.trims):
        if index >= state.active_count:
            break
        slot = state.slots[index]
        if state.is_wall:
            if not slot.has_fragment:
                continue
            assert slot.mark_in is not None
            assert slot.mark_out is not None
            slot.set_marks(slot.mark_in + trim, slot.mark_out + trim)
        else:
            slot.trim_start = max(0.0, trim)
    state.clamp_playhead()
