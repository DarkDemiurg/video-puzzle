from __future__ import annotations

from dataclasses import dataclass

from video_puzzle.state import Slot

LENGTH_WARN_SECONDS = 2.0
LENGTH_WARN_RATIO = 1.25


@dataclass(frozen=True)
class WallPreflight:
    missing_files: list[int]
    missing_fragments: list[int]
    durations: list[float]

    @property
    def min_duration(self) -> float | None:
        return min(self.durations) if self.durations else None

    @property
    def max_duration(self) -> float | None:
        return max(self.durations) if self.durations else None

    @property
    def spread(self) -> float | None:
        if self.min_duration is None or self.max_duration is None:
            return None
        return self.max_duration - self.min_duration

    @property
    def length_warning(self) -> bool:
        if self.min_duration is None or self.max_duration is None:
            return False
        if self.min_duration <= 0:
            return True
        return (
            self.max_duration - self.min_duration >= LENGTH_WARN_SECONDS
            or self.max_duration / self.min_duration >= LENGTH_WARN_RATIO
        )


def clamp_marks(duration: float, mark_in: float, mark_out: float) -> tuple[float, float]:
    start = min(max(0.0, mark_in), max(0.0, duration - 0.04))
    end = min(max(start + 0.04, mark_out), duration)
    if end <= start:
        end = min(duration, start + 0.04)
    return start, end


def inspect_wall(slots: list[Slot], count: int) -> WallPreflight:
    active = slots[:count]
    missing_files = [i for i, slot in enumerate(active) if slot.path is None]
    missing_fragments = [
        i for i, slot in enumerate(active) if slot.path is not None and not slot.has_fragment
    ]
    durations = [duration for slot in active if (duration := slot.fragment_duration) is not None]
    return WallPreflight(
        missing_files=missing_files,
        missing_fragments=missing_fragments,
        durations=durations,
    )


class AutoFragmentError(ValueError):
    """Wall auto-fragment could not be applied."""


def apply_shortest_from_start(slots: list[Slot], count: int) -> float:
    """Set every active clip's fragment to [0, shortest clip duration]."""
    active = slots[:count]
    known: list[float] = []
    for slot in active:
        if slot.path is None:
            raise AutoFragmentError("Заполните все ячейки сетки.")
        if slot.duration is None:
            raise AutoFragmentError("Дождитесь определения длительности всех роликов.")
        known.append(slot.duration)
    span = min(known)
    if span <= 0.04:
        raise AutoFragmentError("Самый короткий ролик слишком короткий для фрагмента.")
    for slot in active:
        slot.set_marks(0.0, span)
    return span
