from pathlib import Path

import pytest

from video_puzzle.layout import grid_cell_size, grid_output_size, xstack_layout
from video_puzzle.state import Slot
from video_puzzle.wall import (
    AutoFragmentError,
    apply_shortest_from_start,
    clamp_marks,
    inspect_wall,
)


def test_xstack_layout_2x2() -> None:
    assert xstack_layout(2, 2) == "0_0|w0_0|0_h0|w0_h0"


def test_xstack_layout_2x3() -> None:
    assert xstack_layout(2, 3) == "0_0|w0_0|w0+w1_0|0_h0|w0_h0|w0+w1_h0"


def test_grid_cells_are_even() -> None:
    cell_w, cell_h = grid_cell_size(3, 2, 1920, 1080)
    assert cell_w % 2 == 0
    assert cell_h % 2 == 0
    assert grid_output_size(3, 2, 1920, 1080) == (cell_w * 3, cell_h * 2)


def test_clamp_marks() -> None:
    start, end = clamp_marks(10.0, -1.0, 40.0)
    assert start == 0.0
    assert end == 10.0
    start, end = clamp_marks(10.0, 8.0, 8.0)
    assert end > start


def test_inspect_wall_missing_fragment_and_length_warning() -> None:
    slots = [
        Slot(path=Path("a.mp4"), mark_in=0.0, mark_out=8.0),
        Slot(path=Path("b.mp4"), mark_in=0.0, mark_out=20.0),
        Slot(path=Path("c.mp4")),
    ]
    report = inspect_wall(slots, 3)
    assert report.missing_fragments == [2]
    assert report.length_warning is True
    assert report.min_duration == 8.0
    assert report.max_duration == 20.0
    assert report.missing_files == []


def test_apply_shortest_from_start_uses_min_clip_length() -> None:
    slots = [
        Slot(path=Path("a.mp4"), duration=10.0, mark_in=2.0, mark_out=9.0),
        Slot(path=Path("b.mp4"), duration=7.5),
        Slot(path=Path("c.mp4"), duration=12.0),
    ]
    span = apply_shortest_from_start(slots, 3)
    assert span == 7.5
    for slot in slots:
        assert slot.mark_in == 0.0
        assert slot.mark_out == 7.5


def test_apply_shortest_from_start_needs_files_and_durations() -> None:
    empty = [Slot(path=Path("a.mp4"), duration=5.0), Slot()]
    with pytest.raises(AutoFragmentError, match="ячейки"):
        apply_shortest_from_start(empty, 2)
    probing = [
        Slot(path=Path("a.mp4"), duration=5.0),
        Slot(path=Path("b.mp4")),
    ]
    with pytest.raises(AutoFragmentError, match="длительности"):
        apply_shortest_from_start(probing, 2)
