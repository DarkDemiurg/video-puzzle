import numpy as np
import pytest

from video_puzzle.state import Slot
from video_puzzle.sync import align_slots, lag_seconds, trims_from_lags


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
    assert "хвосте" in result.summary
