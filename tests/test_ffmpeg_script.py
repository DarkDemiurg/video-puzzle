from pathlib import Path

import pytest

from tests.factories import filled_state, video
from video_puzzle.ffmpeg_script import (
    IncompleteStateError,
    build_ffmpeg_args,
    build_filter_complex,
    render_shell_script,
)
from video_puzzle.layout import AppMode, Layout
from video_puzzle.state import AppState


def test_incomplete_state_cannot_build_args() -> None:
    state = AppState()
    with pytest.raises(IncompleteStateError, match="empty: 1, 2, 3, 4"):
        build_ffmpeg_args(state, Path("out.mp4"))


def test_two_horizontal_uses_hstack() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    graph = build_filter_complex(state)
    assert "[v0][v1]hstack=inputs=2[outv]" in graph
    assert "scale=960:1080" in graph


def test_two_vertical_uses_vstack() -> None:
    graph = build_filter_complex(filled_state(Layout.TWO_VERTICAL))
    assert "[v0][v1]vstack=inputs=2[outv]" in graph
    assert "scale=1920:540" in graph


def test_pyramid_stacks_top_over_bottom_pair() -> None:
    graph = build_filter_complex(filled_state(Layout.THREE_PYRAMID))
    assert "[v1][v2]hstack=inputs=2[bottom]" in graph
    assert "[v0][bottom]vstack=inputs=2[outv]" in graph
    assert "scale=1920:540" in graph
    assert "scale=960:540" in graph


def test_square_is_two_rows() -> None:
    graph = build_filter_complex(filled_state(Layout.FOUR_SQUARE))
    assert "[v0][v1]hstack=inputs=2[top]" in graph
    assert "[v2][v3]hstack=inputs=2[bottom]" in graph
    assert "[top][bottom]vstack=inputs=2[outv]" in graph


def test_args_include_each_input_and_encoder_flags() -> None:
    state = filled_state(Layout.FOUR_SQUARE)
    args = build_ffmpeg_args(state, Path("/tmp/mosaic.mp4"))
    assert args[0] == "ffmpeg"
    assert "-hide_banner" in args
    inputs = [args[i + 1] for i, token in enumerate(args) if token == "-i"]
    assert inputs == [str(video(f"clip{i}")) for i in range(4)]
    assert args[-1] == "/tmp/mosaic.mp4"
    joined = " ".join(args)
    assert args[args.index("-map") + 1] == "[outv]"
    assert "-an" in args
    assert "-shortest" in args
    assert "libx264" in args
    assert "yuv420p" in args
    assert "-filter_complex" in joined


def test_audio_from_first_clip_with_sound_is_mapped() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 8.0, True)
    state.set_probe(1, 8.0, False)
    graph = build_filter_complex(state)
    assert "[0:a]aresample=48000[outa]" in graph
    args = build_ffmpeg_args(state, Path("out.mp4"))
    assert "-an" not in args
    assert args[args.index("-map", args.index("[outv]")) + 1] == "[outa]"
    assert "aac" in args


def test_start_trim_is_applied_to_video_and_audio() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 10.0, True)
    state.set_probe(1, 10.0, False)
    state.slots[0].trim_start = 1.25
    graph = build_filter_complex(state)
    assert "trim=start=1.2500" in graph
    assert "atrim=start=1.2500" in graph
    assert ":end=" not in graph


def test_output_range_adds_start_and_end_trim() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 90.0, True)
    state.set_probe(1, 90.0, False)
    state.set_range_enabled(True)
    state.set_output_range(20.0, 60.0)
    graph = build_filter_complex(state)
    assert "trim=start=20.0000:end=60.0000" in graph
    assert "atrim=start=20.0000:end=60.0000" in graph
    script = render_shell_script(state, Path("out.mp4"))
    assert "20.00s–60.00s" in script


def test_narrow_pyramid_pads_top_cell_to_full_width() -> None:
    graph = build_filter_complex(filled_state(Layout.THREE_PYRAMID_NARROW))
    assert "scale=960:540" in graph
    assert "pad=1920:540" in graph
    assert "[v1][v2]hstack=inputs=2[bottom]" in graph


def test_render_flags_add_overwrite_and_progress() -> None:
    args = build_ffmpeg_args(filled_state(), Path("out.mp4"), overwrite=True, progress=True)
    assert "-y" in args
    assert args[args.index("-progress") + 1] == "pipe:1"


def test_two_and_three_file_commands_use_matching_input_count() -> None:
    two = build_ffmpeg_args(filled_state(Layout.TWO_HORIZONTAL), Path("out.mp4"))
    three = build_ffmpeg_args(filled_state(Layout.THREE_PYRAMID), Path("out.mp4"))
    assert two.count("-i") == 2
    assert three.count("-i") == 3


def test_720p_scales_to_half_of_1280x720() -> None:
    graph = build_filter_complex(filled_state(Layout.FOUR_SQUARE, resolution=720))
    assert "scale=640:360" in graph


def test_script_is_executable_bash() -> None:
    script = render_shell_script(filled_state(), Path("/tmp/out.mp4"))
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script
    assert "ffmpeg" in script
    assert "/tmp/clip0.mp4" in script
    assert "/tmp/out.mp4" in script
    assert script.endswith("\n")


def test_script_quotes_spaces_in_paths() -> None:
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    state.set_slot(0, Path("/tmp/my video.mp4"))
    state.set_slot(1, Path("/tmp/other.mp4"))
    script = render_shell_script(state, Path("/tmp/out file.mp4"))
    assert "'/tmp/my video.mp4'" in script or '"/tmp/my video.mp4"' in script
    assert "'/tmp/out file.mp4'" in script or '"/tmp/out file.mp4"' in script


def test_wall_uses_xstack_and_shortest_fragment() -> None:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 2)
    for index in range(4):
        state.set_slot(index, video(f"clip{index}"))
        state.set_probe(index, 30.0, index == 0)
        state.slots[index].set_marks(1.0, 6.0 if index < 3 else 11.0)
    graph = build_filter_complex(state)
    args = build_ffmpeg_args(state, Path("wall.mp4"))
    assert "xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0" in graph
    assert "trim=start=1.0000:end=6.0000" in graph
    assert args.count("-i") == 4
    script = render_shell_script(state, Path("out.mp4"))
    assert "wall 2x2" in script
    assert "shortest fragment (5.00s)" in script


def test_wall_3x2_uses_even_cell_scale() -> None:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 3)
    for index in range(6):
        state.set_slot(index, video(f"cell{index}"))
        state.set_probe(index, 12.0, False)
        state.slots[index].set_marks(0.0, 4.0)
    graph = build_filter_complex(state)
    assert "scale=640:540" in graph
    assert "xstack=inputs=6" in graph


def test_wall_1x1_passthrough() -> None:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(1, 1)
    state.set_slot(0, video("solo"))
    state.set_probe(0, 8.0, False)
    state.slots[0].set_marks(0.5, 3.5)
    graph = build_filter_complex(state)
    assert "[v0]null[outv]" in graph
    assert "trim=start=0.5000:end=3.5000" in graph
