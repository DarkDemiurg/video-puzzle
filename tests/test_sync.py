import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import numpy as np
import pytest

from video_puzzle.layout import AppMode, Layout
from video_puzzle.state import AppState, Slot
from video_puzzle.sync import (
    AUDIO_EXTRACT_TIMEOUT_SECONDS,
    ClipAlignment,
    SyncError,
    SyncResult,
    align_slots,
    analyze_slots,
    apply_sync,
    extract_mono_pcm,
    lag_seconds,
    trims_from_lags,
)


def _noise(seconds: float, rate: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(int(seconds * rate))


def test_lag_detects_other_started_later() -> None:
    rate = 4000
    reference = _noise(3.0, rate)
    delay = 0.4
    other = reference[int(delay * rate) :]
    lag, score = lag_seconds(reference, other, rate, max_lag_s=1.0)
    assert score > 0.5
    assert lag == pytest.approx(delay, abs=0.02)


def test_lag_detects_other_started_earlier() -> None:
    rate = 4000
    delay = 0.35
    full = _noise(3.5, rate)
    reference = full[int(delay * rate) :]
    lag, score = lag_seconds(reference, full, rate, max_lag_s=1.0)
    assert score > 0.5
    assert lag == pytest.approx(-delay, abs=0.02)


def test_trims_shift_to_latest_start() -> None:
    # Clip 1 started later than clip 0 → cut the extra head of clip 0.
    assert trims_from_lags([0.0, 0.5]) == pytest.approx([0.5, 0.0])
    # Clip 1 started earlier than clip 0 → cut the extra head of clip 1.
    assert trims_from_lags([0.0, -0.4]) == pytest.approx([0.0, 0.4])


def test_align_slots_uses_audio_then_reports_missing_tail() -> None:
    rate = 4000
    reference = _noise(4.0, rate)
    delay = 0.5
    other = reference[int(delay * rate) :]
    slots = [
        Slot(duration=4.0, has_audio=True),
        Slot(duration=3.2, has_audio=True),
    ]
    result = align_slots(slots, pcm=[reference, other], sample_rate=rate, max_lag_s=1.0)
    assert result.used_audio
    assert result.trims[0] == pytest.approx(delay, abs=0.03)
    assert result.trims[1] == pytest.approx(0.0, abs=0.03)
    assert result.alignments[0].missing_tail == pytest.approx(0.3, abs=0.08)
    assert result.alignments[1].missing_tail == pytest.approx(0.0, abs=0.08)


def test_align_without_audio_assumes_shared_start() -> None:
    slots = [
        Slot(duration=10.0, has_audio=False),
        Slot(duration=9.2, has_audio=False),
    ]
    result = align_slots(slots, pcm=[None, None])
    assert not result.used_audio
    assert result.trims == [0.0, 0.0]
    assert result.alignments[0].missing_tail == pytest.approx(0.8)
    assert result.alignments[1].missing_tail == pytest.approx(0.0)
    assert "at the end" in result.summary


def test_extract_mono_pcm_passes_timeout(tmp_path) -> None:
    seen: dict[str, object] = {}

    def runner(cmd, **kwargs):
        seen.update(kwargs)
        pcm = np.zeros(8000, dtype=np.float32).tobytes()
        return CompletedProcess(args=cmd, returncode=0, stdout=pcm, stderr=b"")

    pcm = extract_mono_pcm(tmp_path / "a.mp4", ffmpeg="ffmpeg", runner=runner)
    assert pcm.size == 8000
    assert seen["timeout"] == AUDIO_EXTRACT_TIMEOUT_SECONDS


def test_extract_mono_pcm_timeout_becomes_sync_error(tmp_path) -> None:
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

    with pytest.raises(SyncError, match="took too long"):
        extract_mono_pcm(tmp_path / "a.mp4", ffmpeg="ffmpeg", runner=runner)


def test_extract_mono_pcm_missing_binary_becomes_sync_error(tmp_path) -> None:
    def runner(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(SyncError, match="Could not start ffmpeg"):
        extract_mono_pcm(tmp_path / "a.mp4", ffmpeg="ffmpeg", runner=runner)


def _result(trims: list[float]) -> SyncResult:
    alignments = [
        ClipAlignment(index=i, trim_start=t, missing_tail=0.0, lag=0.0, score=1.0)
        for i, t in enumerate(trims)
    ]
    return SyncResult(
        trims=trims,
        alignments=alignments,
        overlap=10.0,
        used_audio=True,
        summary="test",
    )


def _two_slot_state(wall: bool) -> AppState:
    state = AppState()
    if wall:
        state.set_mode(AppMode.WALL)
        state.set_wall_grid(1, 2)
    else:
        state.layout = Layout.TWO_HORIZONTAL
    for index in range(2):
        state.set_slot(index, Path(f"/tmp/c{index}.mp4"))
        state.set_probe(index, 30.0, True)
    return state


def test_apply_sync_puzzle_writes_start_trims() -> None:
    state = _two_slot_state(wall=False)
    apply_sync(state, _result([0.5, 0.0]))
    assert state.slots[0].trim_start == pytest.approx(0.5)
    assert state.slots[1].trim_start == pytest.approx(0.0)


def test_apply_sync_wall_shifts_fragments_not_trims() -> None:
    """A wall ignores trim_start, so trims there were a silent no-op."""

    state = _two_slot_state(wall=True)
    state.slots[0].set_marks(5.0, 15.0)
    state.slots[1].set_marks(2.0, 20.0)

    apply_sync(state, _result([0.0, 3.0]))

    assert state.slots[0].trim_start == 0.0
    assert state.slots[1].trim_start == 0.0
    assert state.slots[0].mark_in == pytest.approx(5.0)
    assert state.slots[1].mark_in == pytest.approx(5.0)
    assert state.slots[1].mark_out == pytest.approx(23.0)


def test_apply_sync_wall_skips_cells_without_fragment() -> None:
    state = _two_slot_state(wall=True)
    state.slots[0].set_marks(1.0, 9.0)

    apply_sync(state, _result([2.0, 1.0]))

    assert state.slots[0].mark_in == pytest.approx(3.0)
    assert state.slots[1].mark_in is None


def test_align_slots_does_not_trust_a_periodic_signal() -> None:
    """A tone repeats its correlation peak every period, so the lag is ambiguous."""
    rate = 4000
    time_axis = np.arange(int(4.0 * rate)) / rate
    tone = np.sin(2 * np.pi * 220.0 * time_axis)
    inverted = -tone
    slots = [
        Slot(duration=4.0, has_audio=True),
        Slot(duration=4.0, has_audio=True),
    ]

    result = align_slots(slots, pcm=[tone, inverted], sample_rate=rate, max_lag_s=0.5)

    assert result.trims == pytest.approx([0.0, 0.0])
    assert "Weak correlation" in result.summary


def test_align_slots_rejects_unrelated_audio() -> None:
    rate = 4000
    slots = [Slot(duration=3.0, has_audio=True) for _ in range(2)]

    result = align_slots(
        slots, pcm=[_noise(3.0, rate, seed=1), _noise(3.0, rate, seed=2)], sample_rate=rate
    )

    assert result.trims == pytest.approx([0.0, 0.0])
    assert "Weak correlation" in result.summary


def test_align_slots_uses_pairwise_median_offsets() -> None:
    rate = 4000
    reference = _noise(4.0, rate, seed=3)
    later = reference[int(0.4 * rate) :]
    earlier = np.concatenate([_noise(0.2, rate, seed=4), reference])[: reference.size]
    slots = [Slot(duration=4.0, has_audio=True) for _ in range(3)]

    result = align_slots(slots, pcm=[reference, later, earlier], sample_rate=rate, max_lag_s=1.0)

    # `later` starts 0.4 s after the reference, `earlier` 0.2 s before it.
    assert result.trims[0] == pytest.approx(0.4, abs=0.05)
    assert result.trims[1] == pytest.approx(0.0, abs=0.05)
    assert result.trims[2] == pytest.approx(0.6, abs=0.05)


def test_analyze_slots_reports_unreadable_audio(monkeypatch, tmp_path: Path) -> None:
    import video_puzzle.sync as sync_module

    def fake_extract(path: Path, *, ffmpeg=None, runner=None, timeout=0.0):
        if path.name == "bad.mp4":
            raise SyncError("cannot decode")
        return _noise(3.0, 4000, seed=5)

    monkeypatch.setattr(sync_module, "extract_mono_pcm", fake_extract)
    slots = [
        Slot(path=tmp_path / "good.mp4", duration=3.0, has_audio=True),
        Slot(path=tmp_path / "bad.mp4", duration=3.0, has_audio=True),
    ]

    result = analyze_slots(slots)

    assert result.audio_errors == (1,)
    assert "Audio could not be read" in result.summary
