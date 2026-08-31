from dataclasses import dataclass, field
from pathlib import Path

from video_puzzle.encode import DEFAULT_ENCODER, DEFAULT_QUALITY, EncodeQuality, EncoderKind
from video_puzzle.layout import (
    MAX_GRID,
    MAX_SLOTS,
    VIDEO_EXTENSIONS,
    AppMode,
    Layout,
    canvas_size,
    even,
    slot_count,
)


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


@dataclass
class Slot:
    path: Path | None = None
    duration: float | None = None
    has_audio: bool = False
    fps: float | None = None
    trim_start: float = 0.0
    mark_in: float | None = None
    mark_out: float | None = None
    rotation: int = 0
    crop: tuple[float, float, float, float] | None = None

    def clear(self) -> None:
        self.path = None
        self.duration = None
        self.has_audio = False
        self.fps = None
        self.trim_start = 0.0
        self.mark_in = None
        self.mark_out = None
        self.rotation = 0
        self.crop = None

    def set_path(self, path: Path) -> None:
        if self.path != path:
            self.trim_start = 0.0
            self.duration = None
            self.has_audio = False
            self.fps = None
            self.mark_in = None
            self.mark_out = None
            self.rotation = 0
            self.crop = None
        self.path = path

    def set_marks(self, mark_in: float, mark_out: float) -> None:
        duration = self.duration if self.duration is not None else mark_out
        start = min(max(0.0, mark_in), max(0.0, duration - 0.04))
        end = min(max(start + 0.04, mark_out), duration if self.duration is not None else mark_out)
        self.mark_in = start
        self.mark_out = end

    def clear_marks(self) -> None:
        self.mark_in = None
        self.mark_out = None

    @property
    def has_fragment(self) -> bool:
        return (
            self.mark_in is not None
            and self.mark_out is not None
            and self.mark_out > self.mark_in + 0.001
        )

    @property
    def fragment_duration(self) -> float | None:
        if not self.has_fragment:
            return None
        assert self.mark_in is not None
        assert self.mark_out is not None
        return self.mark_out - self.mark_in

    @property
    def effective_duration(self) -> float | None:
        if self.duration is None:
            return None
        return max(0.0, self.duration - self.trim_start)


@dataclass
class AppState:
    layout: Layout = Layout.FOUR_SQUARE
    resolution: int = 1080
    quality: EncodeQuality = DEFAULT_QUALITY
    encoder: EncoderKind = DEFAULT_ENCODER
    mode: AppMode = AppMode.PUZZLE
    wall_rows: int = 2
    wall_cols: int = 2
    slots: list[Slot] = field(default_factory=lambda: [Slot() for _ in range(MAX_SLOTS)])
    playhead: float = 0.0
    include_audio: bool = True
    audio_slot: int | None = None
    normalize_audio: bool = False
    cell_gap: int = 0
    range_enabled: bool = False
    range_start: float = 0.0
    range_end: float | None = None

    def __post_init__(self) -> None:
        if len(self.slots) != MAX_SLOTS:
            raise ValueError(f"Expected {MAX_SLOTS} slots, got {len(self.slots)}")
        canvas_size(self.resolution)
        self._clamp_wall()
        self.quality = EncodeQuality(self.quality)
        self.encoder = EncoderKind(self.encoder)
        self.cell_gap = even(max(0, min(40, self.cell_gap)))
        if self.audio_slot is not None and not 0 <= self.audio_slot < MAX_SLOTS:
            self.audio_slot = None

    @property
    def is_wall(self) -> bool:
        return self.mode is AppMode.WALL

    @property
    def active_count(self) -> int:
        if self.is_wall:
            return self.wall_rows * self.wall_cols
        return slot_count(self.layout)

    def set_mode(self, mode: AppMode) -> None:
        self.mode = mode
        self.clamp_playhead()

    def set_wall_grid(self, rows: int, cols: int) -> None:
        self.wall_rows = rows
        self.wall_cols = cols
        self._clamp_wall()
        self.clamp_playhead()

    def _clamp_wall(self) -> None:
        self.wall_rows = min(MAX_GRID, max(1, self.wall_rows))
        self.wall_cols = min(MAX_GRID, max(1, self.wall_cols))

    def set_layout(self, layout: Layout) -> None:
        self.layout = layout
        self.clamp_playhead()

    def set_resolution(self, height: int) -> None:
        canvas_size(height)
        self.resolution = height

    def set_quality(self, quality: EncodeQuality) -> None:
        self.quality = EncodeQuality(quality)

    def set_encoder(self, encoder: EncoderKind) -> None:
        self.encoder = EncoderKind(encoder)

    def set_cell_gap(self, gap: int) -> None:
        self.cell_gap = even(max(0, min(40, gap)))

    def swap_slots(self, first: int, second: int) -> None:
        if first == second:
            return
        if not 0 <= first < MAX_SLOTS or not 0 <= second < MAX_SLOTS:
            raise IndexError("Slot index is out of range")
        self.slots[first], self.slots[second] = self.slots[second], self.slots[first]
        self.clamp_playhead()

    def set_slot(self, index: int, path: Path | None) -> None:
        if not 0 <= index < MAX_SLOTS:
            raise IndexError(f"Slot index {index} is out of range")
        if path is None:
            self.slots[index].clear()
            self.clamp_playhead()
            return
        if not is_video_file(path):
            raise ValueError(f"Unsupported video type: {path.suffix}")
        self.slots[index].set_path(path)

    def clear_slot(self, index: int) -> None:
        self.set_slot(index, None)

    def set_probe(
        self, index: int, duration: float, has_audio: bool, fps: float | None = None
    ) -> None:
        if not 0 <= index < MAX_SLOTS:
            raise IndexError(f"Slot index {index} is out of range")
        self.slots[index].duration = max(0.0, duration)
        self.slots[index].has_audio = has_audio
        self.slots[index].fps = fps if fps is not None and fps > 0 else None
        self.clamp_playhead()

    def assign_paths(self, paths: list[Path], start: int = 0) -> int:
        """Fill empty active slots starting at `start`. Returns how many were assigned."""
        assigned = 0
        index = start
        for candidate in paths:
            if not is_video_file(candidate):
                continue
            while index < self.active_count and self.slots[index].path is not None:
                index += 1
            if index >= self.active_count:
                break
            self.slots[index].set_path(candidate)
            assigned += 1
            index += 1
        return assigned

    def active_slots(self) -> list[Slot]:
        return self.slots[: self.active_count]

    def active_paths(self) -> list[Path]:
        return [slot.path for slot in self.active_slots() if slot.path is not None]

    def missing_slot_indexes(self) -> list[int]:
        return [i for i, slot in enumerate(self.active_slots()) if slot.path is None]

    def is_complete(self) -> bool:
        return not self.missing_slot_indexes()

    def overlap_duration(self) -> float | None:
        """Common timeline length after start trims, or None if a duration is still unknown."""
        if self.missing_slot_indexes():
            return None
        lengths: list[float] = []
        for slot in self.active_slots():
            if slot.effective_duration is None:
                return None
            lengths.append(slot.effective_duration)
        return min(lengths) if lengths else None

    def duration_spread(self) -> float | None:
        durations = [slot.duration for slot in self.active_slots() if slot.duration is not None]
        if len(durations) < 2:
            return None
        return max(durations) - min(durations)

    def missing_tail(self, index: int) -> float:
        overlap = self.overlap_duration()
        effective = self.slots[index].effective_duration
        if overlap is None or effective is None:
            return 0.0
        return max(0.0, effective - overlap)

    def wall_output_duration(self) -> float | None:
        lengths = [
            duration
            for slot in self.active_slots()
            if (duration := slot.fragment_duration) is not None
        ]
        if len(lengths) != self.active_count:
            return None
        return min(lengths) if lengths else None

    def clamp_playhead(self) -> None:
        overlap = self.overlap_duration()
        if overlap is None:
            self.playhead = max(0.0, self.playhead)
        else:
            limit = max(0.0, overlap - 0.04)
            self.playhead = min(max(0.0, self.playhead), limit)
        self.clamp_range()

    def clamp_range(self) -> None:
        overlap = self.overlap_duration()
        self.range_start = max(0.0, self.range_start)
        if not self.range_enabled:
            return
        if self.range_end is None:
            self.range_end = overlap
        if overlap is not None:
            self.range_start = min(self.range_start, max(0.0, overlap - 0.04))
            if self.range_end is not None:
                self.range_end = min(max(self.range_end, self.range_start + 0.04), overlap)
                if self.range_end <= self.range_start:
                    self.range_end = overlap

    def set_range_enabled(self, enabled: bool) -> None:
        self.range_enabled = enabled
        if enabled and self.range_end is None:
            self.range_end = self.overlap_duration()
        self.clamp_range()

    def set_output_range(self, start: float, end: float | None) -> None:
        self.range_start = start
        self.range_end = end
        self.clamp_range()

    def resolved_range_start(self) -> float:
        if not self.range_enabled:
            return 0.0
        return max(0.0, self.range_start)

    def resolved_range_end(self) -> float | None:
        if not self.range_enabled:
            return None
        overlap = self.overlap_duration()
        if self.range_end is None:
            return overlap
        if overlap is None:
            return self.range_end
        return min(self.range_end, overlap)

    def export_duration(self) -> float | None:
        if self.is_wall:
            return self.wall_output_duration()
        start = self.resolved_range_start()
        end = self.resolved_range_end()
        if end is not None:
            return max(0.0, end - start)
        overlap = self.overlap_duration()
        if overlap is None:
            return None
        return max(0.0, overlap - start)

    def source_window(self, index: int) -> tuple[float, float | None]:
        """Start (and optional end) inside the source file."""
        slot = self.slots[index]
        if self.is_wall:
            if not slot.has_fragment:
                return 0.0, None
            assert slot.mark_in is not None
            span = self.wall_output_duration()
            if span is None:
                return slot.mark_in, slot.mark_out
            return slot.mark_in, slot.mark_in + span
        start = slot.trim_start + self.resolved_range_start()
        aligned_end = self.resolved_range_end()
        end = None if aligned_end is None else slot.trim_start + aligned_end
        if slot.duration is not None:
            start = min(max(0.0, start), max(0.0, slot.duration - 0.04))
            if end is not None:
                end = min(end, slot.duration)
                if end <= start:
                    end = min(slot.duration, start + 0.04)
        return max(0.0, start), end

    def set_playhead(self, seconds: float) -> None:
        self.playhead = seconds
        self.clamp_playhead()

    def file_time(self, index: int) -> float:
        """Source timestamp for the shared playhead or wall in-point."""
        slot = self.slots[index]
        if self.is_wall:
            if slot.mark_in is not None:
                return slot.mark_in
            return 0.0
        time = self.playhead + slot.trim_start
        if slot.duration is not None:
            time = min(time, max(0.0, slot.duration - 0.04))
        return max(0.0, time)

    def output_fps(self) -> float:
        rates = [slot.fps for slot in self.active_slots() if slot.fps]
        if not rates:
            return 30.0
        return max(rates)

    def audio_input_index(self) -> int | None:
        if not self.include_audio:
            return None
        if self.audio_slot is not None:
            if 0 <= self.audio_slot < self.active_count:
                slot = self.slots[self.audio_slot]
                if slot.path is not None and slot.has_audio:
                    return self.audio_slot
            return None
        for index, slot in enumerate(self.active_slots()):
            if slot.path is not None and slot.has_audio:
                return index
        return None
