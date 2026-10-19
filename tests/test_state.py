from pathlib import Path

import pytest

from tests.factories import filled_state, video
from video_puzzle.encode import EncodeQuality
from video_puzzle.layout import MAX_SLOTS, AppMode, Layout
from video_puzzle.state import AppState, Slot, is_video_file


def test_new_state_has_four_empty_slots() -> None:
    state = AppState()
    assert state.layout is Layout.FOUR_SQUARE
    assert state.resolution == 1080
    assert state.quality is EncodeQuality.STANDARD
    assert all(slot.path is None for slot in state.slots)
    assert not state.is_complete()
    assert state.missing_slot_indexes() == [0, 1, 2, 3]


def test_is_video_file() -> None:
    assert is_video_file(Path("a.mp4"))
    assert is_video_file(Path("a.MKV"))
    assert not is_video_file(Path("a.txt"))
    assert not is_video_file(Path("a"))


def test_rejects_bad_quality() -> None:
    with pytest.raises(ValueError):
        AppState(quality="ultra")  # type: ignore[arg-type]


def test_rejects_wrong_slot_count() -> None:
    with pytest.raises(ValueError, match=f"Expected {MAX_SLOTS} slots"):
        AppState(slots=[Slot()])


def test_set_slot_rejects_unknown_type() -> None:
    state = AppState()
    with pytest.raises(ValueError, match="Unsupported video type"):
        state.set_slot(0, Path("notes.txt"))


def test_set_slot_rejects_bad_index() -> None:
    state = AppState()
    with pytest.raises(IndexError):
        state.set_slot(MAX_SLOTS, video("a"))


def test_complete_when_active_slots_filled() -> None:
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    state.set_slot(0, video("a"))
    assert not state.is_complete()
    state.set_slot(1, video("b"))
    assert state.is_complete()
    assert state.active_paths() == [video("a"), video("b")]


def test_inactive_slots_ignored_for_completeness() -> None:
    state = filled_state(Layout.FOUR_SQUARE)
    state.set_layout(Layout.TWO_VERTICAL)
    assert state.is_complete()
    assert state.active_count == 2
    assert state.slots[2].path is not None


def test_clear_slot() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.clear_slot(0)
    assert state.slots[0].path is None
    assert state.missing_slot_indexes() == [0]


def test_assign_paths_fills_empty_slots_in_order() -> None:
    state = AppState()
    state.set_slot(1, video("kept"))
    assigned = state.assign_paths([video("a"), video("b"), video("c")])
    assert assigned == 3
    assert state.slots[0].path == video("a")
    assert state.slots[1].path == video("kept")
    assert state.slots[2].path == video("b")
    assert state.slots[3].path == video("c")


def test_assign_paths_skips_non_video_and_stops_at_capacity() -> None:
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    assigned = state.assign_paths(
        [Path("readme.txt"), video("a"), Path("photo.jpg"), video("b"), video("c")]
    )
    assert assigned == 2
    assert [slot.path for slot in state.slots[:2]] == [video("a"), video("b")]
    assert state.slots[2].path is None


def test_assign_paths_from_start_index() -> None:
    state = AppState()
    assigned = state.assign_paths([video("a"), video("b")], start=2)
    assert assigned == 2
    assert [slot.path for slot in state.slots[:4]] == [None, None, video("a"), video("b")]


def test_assign_paths_never_walks_backwards() -> None:
    """Extras of a drop on the last cell must not land in the cells before it.

    Filling used to start from slot 0, silently replacing clips the user picked by
    hand; now the extras simply do not fit and the caller reports the leftover.
    """

    state = AppState(layout=Layout.FOUR_SQUARE)
    for index, name in enumerate(("keep0", "keep1", "keep2")):
        state.set_slot(index, video(name))
    assigned = state.assign_paths([video("x"), video("y")], start=3)
    assert assigned == 1
    assert [state.slots[i].path for i in range(4)] == [
        video("keep0"),
        video("keep1"),
        video("keep2"),
        video("x"),
    ]
    assert state.assign_paths([video("y")], start=4) == 0


def test_assign_paths_stops_at_scheme_edge_not_slot_array() -> None:
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    assigned = state.assign_paths([video("a"), video("b"), video("c")], start=0)
    assert assigned == 2
    assert [slot.path for slot in state.slots[:4]] == [video("a"), video("b"), None, None]


def test_overlap_and_file_time_use_trim() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 10.0, True)
    state.set_probe(1, 8.0, True)
    state.slots[0].trim_start = 1.0
    state.set_playhead(2.0)
    assert state.overlap_duration() == pytest.approx(8.0)
    assert state.file_time(0) == pytest.approx(3.0)
    assert state.file_time(1) == pytest.approx(2.0)
    assert state.missing_tail(0) == pytest.approx(1.0)
    assert state.missing_tail(1) == pytest.approx(0.0)


def test_playhead_clamps_to_overlap() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 5.0, False)
    state.set_probe(1, 5.0, False)
    state.set_playhead(99.0)
    assert state.playhead == pytest.approx(4.96)


def test_changing_path_resets_trim_and_probe() -> None:
    state = AppState()
    state.set_slot(0, video("a"))
    state.set_probe(0, 12.0, True)
    state.slots[0].trim_start = 1.5
    state.set_slot(0, video("b"))
    assert state.slots[0].duration is None
    assert state.slots[0].trim_start == 0.0
    assert state.slots[0].has_audio is False


def test_audio_input_prefers_first_clip_with_sound() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 5.0, False)
    state.set_probe(1, 5.0, True)
    assert state.audio_input_index() == 1
    state.include_audio = False
    assert state.audio_input_index() is None


def test_output_range_is_optional_and_on_aligned_timeline() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 100.0, True)
    state.set_probe(1, 100.0, True)
    state.slots[0].trim_start = 1.25
    start, end = state.source_window(0)
    assert start == pytest.approx(1.25)
    assert end is None
    state.set_range_enabled(True)
    state.set_output_range(20.0, 60.0)
    start, end = state.source_window(0)
    assert start == pytest.approx(21.25)
    assert end == pytest.approx(61.25)
    start1, end1 = state.source_window(1)
    assert start1 == pytest.approx(20.0)
    assert end1 == pytest.approx(60.0)
    assert state.export_duration() == pytest.approx(40.0)


def test_output_range_clamps_to_overlap() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 50.0, False)
    state.set_probe(1, 50.0, False)
    state.set_range_enabled(True)
    state.set_output_range(20.0, 80.0)
    assert state.resolved_range_start() == pytest.approx(20.0)
    assert state.resolved_range_end() == pytest.approx(50.0)
    assert state.export_duration() == pytest.approx(30.0)


def test_disabling_range_drops_end_trim() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 80.0, False)
    state.set_probe(1, 80.0, False)
    state.set_range_enabled(True)
    state.set_output_range(20.0, 60.0)
    state.set_range_enabled(False)
    assert state.resolved_range_start() == 0.0
    assert state.resolved_range_end() is None
    assert state.source_window(0)[1] is None


def test_wall_grid_changes_active_count() -> None:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(3, 4)
    assert state.active_count == 12
    assert state.is_wall


def test_wall_source_window_uses_shortest_fragment() -> None:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(1, 2)
    state.set_slot(0, video("a"))
    state.set_slot(1, video("b"))
    state.set_probe(0, 30.0, False)
    state.set_probe(1, 30.0, False)
    state.slots[0].set_marks(5.0, 15.0)
    state.slots[1].set_marks(2.0, 20.0)
    assert state.wall_output_duration() == pytest.approx(10.0)
    start0, end0 = state.source_window(0)
    start1, end1 = state.source_window(1)
    assert start0 == pytest.approx(5.0)
    assert end0 == pytest.approx(15.0)
    assert start1 == pytest.approx(2.0)
    assert end1 == pytest.approx(12.0)
    assert state.file_time(0) == pytest.approx(5.0)


def test_wall_without_fragments_is_complete_but_not_ready() -> None:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 2)
    for index in range(4):
        state.set_slot(index, video(f"clip{index}"))
        state.set_probe(index, 30.0, False)
    assert state.is_complete()
    assert state.missing_fragment_indexes() == [0, 1, 2, 3]
    assert not state.is_ready_for_export()

    state.slots[1].set_marks(3.0, 9.0)
    assert state.missing_fragment_indexes() == [0, 2, 3]
    for index in (0, 2, 3):
        state.slots[index].set_marks(0.0, 4.0)
    assert state.missing_fragment_indexes() == []
    assert state.is_ready_for_export()


def test_puzzle_mode_needs_no_fragments() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    for index in range(state.active_count):
        state.set_probe(index, 10.0, False)
    assert state.missing_fragment_indexes() == []
    assert state.is_ready_for_export()


def test_empty_wall_cells_are_not_missing_fragments() -> None:
    """A cell with no file is a missing *file*, not a missing mark."""

    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 2)
    state.set_slot(0, video("a"))
    state.slots[0].set_marks(1.0, 5.0)
    assert state.missing_fragment_indexes() == []
    assert state.missing_slot_indexes() == [1, 2, 3]
    assert not state.is_ready_for_export()


def test_output_fps_uses_highest_source() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    assert state.output_fps() == pytest.approx(30.0)
    state.set_probe(0, 5.0, False, fps=24.0)
    state.set_probe(1, 5.0, False, fps=29.97)
    assert state.output_fps() == pytest.approx(29.97)


def test_swap_slots_exchanges_files() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    first, second = state.slots[0].path, state.slots[1].path
    state.swap_slots(0, 1)
    assert state.slots[0].path == second
    assert state.slots[1].path == first


def test_audio_slot_overrides_first_with_sound() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 5.0, True)
    state.set_probe(1, 5.0, True)
    assert state.audio_input_index() == 0
    state.audio_slot = 1
    assert state.audio_input_index() == 1
    state.audio_slot = 1
    state.slots[1].has_audio = False
    assert state.audio_input_index() is None


def test_set_audio_slot_rejects_index_outside_scheme() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    with pytest.raises(IndexError):
        state.set_audio_slot(2)
    assert state.audio_slot is None
    state.set_audio_slot(None)
    assert state.audio_slot is None


def test_shrinking_scheme_drops_dead_audio_pin_and_keeps_sound() -> None:
    state = filled_state(Layout.FOUR_SQUARE)
    for index in range(4):
        state.set_probe(index, 6.0, index == 3)
    state.set_audio_slot(3)
    assert state.audio_input_index() == 3

    state.set_layout(Layout.TWO_HORIZONTAL)

    # Slot 3 is gone: keeping the pin would silently render "-an" while the UI still
    # showed clip 4 as the audio source.
    assert state.audio_slot is None
    assert state.audio_input_index() is None
    state.set_probe(1, 6.0, True)
    assert state.audio_input_index() == 1


def test_wall_grid_shrink_drops_dead_audio_pin() -> None:
    state = filled_state()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 2)
    state.set_audio_slot(3)
    state.set_wall_grid(1, 2)
    assert state.audio_slot is None


def test_swap_slots_follows_the_pinned_audio() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 5.0, True)
    state.set_probe(1, 5.0, False)
    state.set_audio_slot(0)
    state.swap_slots(0, 1)
    assert state.audio_slot == 1
    assert state.audio_input_index() == 1


def test_replacing_pinned_clip_drops_the_pin() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(1, 5.0, True)
    state.set_audio_slot(1)
    state.set_slot(1, video("other"))
    assert state.audio_slot is None

    state.set_audio_slot(1)
    state.clear_slot(1)
    assert state.audio_slot is None


def test_filling_slots_keeps_audio_pin_and_makes_it_effective() -> None:
    """An empty slot must not kill the pin, or the choice could never take effect."""
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    state.set_audio_slot(1)
    clip_a = video("a")
    clip_b = video("b")
    state.set_slot(0, clip_a)
    state.set_slot(1, clip_b)
    assert state.audio_slot == 1
    state.set_probe(0, 5.0, True)
    state.set_probe(1, 5.0, True)
    assert state.audio_input_index() == 1


def test_cell_gap_is_even_and_clamped() -> None:
    state = AppState()
    state.set_cell_gap(7)
    assert state.cell_gap == 6
    state.set_cell_gap(99)
    assert state.cell_gap == 40


def test_probe_clamps_marks_that_exceed_the_duration() -> None:
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    state.set_slot(0, video("clip"))
    state.set_slot(1, video("other"))
    state.slots[0].set_marks(5.0, 30.0)
    state.slots[1].set_marks(0.0, 4.0)

    state.set_probe(0, 10.0, False)
    state.set_probe(1, 10.0, False)

    assert state.slots[0].mark_in == pytest.approx(5.0)
    assert state.slots[0].mark_out == pytest.approx(10.0)
    assert state.slots[0].has_fragment


def test_probe_clears_marks_that_start_after_the_end() -> None:
    """20–30 s on a 10 s clip used to look export-ready and produce an empty file."""

    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(1, 1)
    state.set_slot(0, video("clip"))
    state.slots[0].set_marks(20.0, 30.0)

    state.set_probe(0, 10.0, False)

    assert not state.slots[0].has_fragment
    assert state.slots[0].mark_in is None
    assert state.wall_output_duration() is None
    assert not state.is_ready_for_export()


def test_export_waits_for_a_successful_probe() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    assert not state.is_ready_for_export(), "paths alone are not a readable video"

    state.set_probe(0, 10.0, False)
    assert not state.is_ready_for_export()

    state.set_probe(1, 10.0, False)
    assert state.is_ready_for_export()


def test_audio_only_file_is_not_export_ready() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 10.0, True, has_video=False)
    state.set_probe_error(0, "The file has no video stream")
    state.set_probe(1, 10.0, False)
    assert not state.is_ready_for_export()
    assert state.slots[0].probe_error is not None
