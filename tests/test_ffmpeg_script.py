import shutil
import subprocess
from pathlib import Path

import pytest

from tests.factories import filled_state, video
from video_puzzle.encode import EncodeQuality, EncoderKind
from video_puzzle.ffmpeg_script import (
    IncompleteStateError,
    build_ffmpeg_args,
    build_filter_complex,
    build_still_args,
    mosaic_output_size,
    render_shell_script,
)
from video_puzzle.layout import AppMode, Layout
from video_puzzle.state import AppState


def _seek_for(args: list[str], path: str) -> tuple[str | None, str | None]:
    index = args.index(path)
    assert args[index - 1] == "-i"
    start: str | None = None
    duration: str | None = None
    pos = index - 1
    while pos >= 2 and args[pos - 2] in {"-ss", "-t"}:
        key, value = args[pos - 2], args[pos - 1]
        if key == "-ss":
            start = value
        else:
            duration = value
        pos -= 2
    return start, duration


def _wall_without_marks(*, mark: tuple[int, ...] = ()) -> AppState:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 2)
    for index in range(4):
        state.set_slot(index, video(f"clip{index}"))
        state.set_probe(index, 60.0, False)
    for index in mark:
        state.slots[index].set_marks(1.0, 5.0)
    return state


def test_incomplete_state_cannot_build_args() -> None:
    state = AppState()
    with pytest.raises(IncompleteStateError, match="empty: 1, 2, 3, 4"):
        build_ffmpeg_args(state, Path("out.mp4"))


def test_export_refuses_wall_cells_without_fragments() -> None:
    """The bug: an all-files-but-no-marks wall produced a full-length mosaic.

    ``-shortest`` then matched the longest *untrimmed* clip, so a 6-hour source
    meant a 6-hour video instead of the intended fragment.
    """

    state = _wall_without_marks()
    for build in (
        lambda: build_ffmpeg_args(state, Path("out.mp4")),
        lambda: build_still_args(state, Path("still.jpg")),
        lambda: render_shell_script(state, Path("out.mp4")),
    ):
        with pytest.raises(IncompleteStateError, match="Mark a fragment.*unset: 1, 2, 3, 4"):
            build()


def test_export_names_the_cells_that_still_need_marks() -> None:
    state = _wall_without_marks(mark=(0, 2))
    with pytest.raises(IncompleteStateError, match="unset: 2, 4"):
        build_ffmpeg_args(state, Path("out.mp4"))


def test_export_accepts_wall_with_fragments_in_every_cell() -> None:
    state = _wall_without_marks(mark=(0, 1, 2, 3))
    args = build_ffmpeg_args(state, Path("out.mp4"))
    assert args[-1] == "out.mp4"
    assert "-shortest" in args


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
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "medium"
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
    assert "192k" in args


def test_start_trim_seeks_the_input() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 10.0, True)
    state.set_probe(1, 10.0, False)
    state.slots[0].trim_start = 1.25
    graph = build_filter_complex(state)
    assert "trim=" not in graph
    assert "atrim=" not in graph
    args = build_ffmpeg_args(state, Path("out.mp4"))
    assert _seek_for(args, "/tmp/clip0.mp4") == ("1.2500", None)
    assert _seek_for(args, "/tmp/clip1.mp4") == (None, None)


def test_output_range_seeks_each_input() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 90.0, True)
    state.set_probe(1, 90.0, False)
    state.set_range_enabled(True)
    state.set_output_range(20.0, 60.0)
    graph = build_filter_complex(state)
    assert "trim=" not in graph
    args = build_ffmpeg_args(state, Path("out.mp4"))
    assert _seek_for(args, "/tmp/clip0.mp4") == ("20.0000", "40.0000")
    assert _seek_for(args, "/tmp/clip1.mp4") == ("20.0000", "40.0000")
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


def test_script_overwrites_without_prompting() -> None:
    # Re-running a saved script is the normal workflow: without -y ffmpeg stops and
    # asks "File exists. Overwrite?", which hangs any non-interactive launch.
    script = render_shell_script(filled_state(), Path("/tmp/out.mp4"))
    command = next(line for line in script.splitlines() if line.startswith("ffmpeg "))
    assert " -y " in command
    assert "-n " not in command


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
    assert "trim=" not in graph
    assert args.count("-i") == 4
    assert _seek_for(args, "/tmp/clip0.mp4") == ("1.0000", "5.0000")
    assert _seek_for(args, "/tmp/clip3.mp4") == ("1.0000", "5.0000")
    script = render_shell_script(state, Path("out.mp4"))
    assert "wall 2x2" in script
    assert "shortest fragment (5.00s)" in script


def test_original_quality_lowers_crf_and_raises_audio_bitrate() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 8.0, True)
    state.set_quality(EncodeQuality.ORIGINAL)
    args = build_ffmpeg_args(state, Path("out.mp4"))
    assert args[args.index("-crf") + 1] == "12"
    assert args[args.index("-preset") + 1] == "slow"
    assert args[args.index("-b:a") + 1] == "320k"
    script = render_shell_script(state, Path("out.mp4"))
    assert "Quality: original  encoder=libx264  crf=12" in script


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
    args = build_ffmpeg_args(state, Path("solo.mp4"))
    assert "[v0]null[outv]" in graph
    assert "trim=" not in graph
    assert _seek_for(args, "/tmp/solo.mp4") == ("0.5000", "3.0000")


def test_crop_and_rotate_are_in_filter() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.slots[0].rotation = 90
    state.slots[0].crop = (0.1, 0.2, 0.4, 0.5)
    graph = build_filter_complex(state)
    assert "transpose=1" in graph
    assert "crop=iw*0.4000:ih*0.5000:iw*0.1000:ih*0.2000" in graph


def test_loudnorm_and_explicit_audio_slot() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 8.0, True)
    state.set_probe(1, 8.0, True)
    state.audio_slot = 1
    state.normalize_audio = True
    graph = build_filter_complex(state)
    assert "[1:a]" in graph
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in graph


def test_gap_grows_pad_and_output_size() -> None:
    state = filled_state(Layout.FOUR_SQUARE)
    state.set_cell_gap(10)
    graph = build_filter_complex(state)
    assert "pad=970:550" in graph
    # One 10 px gap sits between the cells; there is no outer border.
    assert "crop=iw-10:ih-10:0:0[outv]" in graph
    assert mosaic_output_size(state) == (1930, 1090)


@pytest.mark.parametrize("layout", list(Layout))
@pytest.mark.parametrize("gap", [0, 6, 40])
def test_gap_pads_match_declared_output_size(layout: Layout, gap: int) -> None:
    """Every pad width/height in the graph must add up to the size we advertise."""
    state = filled_state(layout)
    state.set_cell_gap(gap)
    graph = build_filter_complex(state)
    pads = _pad_boxes(graph)
    assert len(pads) == state.active_count
    out_w, out_h = mosaic_output_size(state)
    assert out_w % 2 == 0 and out_h % 2 == 0
    if layout in {Layout.THREE_PYRAMID, Layout.THREE_PYRAMID_NARROW}:
        # vstack needs the pyramid top as wide as the two bottom cells together.
        assert pads[0][0] == pads[1][0] + pads[2][0]
        assert out_w == pads[1][0] + pads[2][0] - gap
        assert out_h == pads[0][1] + pads[1][1] - gap
    elif layout is Layout.TWO_HORIZONTAL:
        assert out_w == pads[0][0] + pads[1][0] - gap
    elif layout is Layout.TWO_VERTICAL:
        assert out_h == pads[0][1] + pads[1][1] - gap
    elif layout is Layout.FOUR_SQUARE:
        assert out_w == pads[0][0] + pads[1][0] - gap
        assert out_h == pads[0][1] + pads[2][1] - gap


def _pad_boxes(graph: str) -> list[tuple[int, int]]:
    boxes: list[tuple[int, int]] = []
    for part in graph.split(";"):
        marker = ",pad="
        if marker not in part:
            continue
        fields = part.split(marker, 1)[1].split(":")
        boxes.append((int(fields[0]), int(fields[1])))
    return boxes


def test_pyramid_with_gap_renders_in_real_ffmpeg(tmp_path: Path) -> None:
    """Regression: mismatched pad widths made vstack fail the whole graph."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")
    sources = []
    for index in range(3):
        clip = tmp_path / f"src{index}.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={'red green blue'.split()[index]}:s=160x120:r=25:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
        )
        sources.append(clip)
    for layout in (Layout.THREE_PYRAMID, Layout.THREE_PYRAMID_NARROW):
        state = AppState(layout=layout)
        for index, clip in enumerate(sources):
            state.set_slot(index, clip)
            state.set_probe(index, 1.0, False, fps=25.0)
        state.set_cell_gap(6)
        out_w, out_h = mosaic_output_size(state)
        dest = tmp_path / f"{layout}.jpg"
        result = subprocess.run(
            build_still_args(state, dest),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert dest.is_file() and dest.stat().st_size > 0
        probe = subprocess.run(
            [
                shutil.which("ffprobe") or "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(dest),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip() == f"{out_w}x{out_h}"


def test_output_fps_comes_from_sources() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_probe(0, 8.0, False, fps=24.0)
    state.set_probe(1, 8.0, False, fps=60.0)
    graph = build_filter_complex(state)
    assert "fps=60" in graph


def test_nvenc_used_when_available() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.set_encoder(EncoderKind.AUTO)
    args = build_ffmpeg_args(state, Path("out.mp4"), available_encoders={"h264_nvenc", "libx264"})
    assert "h264_nvenc" in args
    assert "libx264" not in args


def test_still_args_request_one_jpeg_frame() -> None:
    args = build_still_args(filled_state(), Path("/tmp/still.jpg"))
    assert args[args.index("-frames:v") + 1] == "1"
    assert args[-1] == "/tmp/still.jpg"


def test_still_args_seek_to_wall_fragment() -> None:
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(1, 1)
    state.set_slot(0, video("solo"))
    state.set_probe(0, 80.0, False)
    state.slots[0].set_marks(12.0, 20.0)
    args = build_still_args(state, Path("/tmp/still.jpg"))
    assert _seek_for(args, "/tmp/solo.mp4") == ("12.0000", "8.0000")


def test_wall_script_reports_fragments_not_start_trims() -> None:
    """Wall windows come from the I/O marks.

    The header used to print "# Start trims: 1=0.00s, ..." while the command itself
    carried the mark offsets, so the comment claimed an offset that -ss did not use.
    """

    state = _wall_without_marks(mark=(0, 1, 2, 3))
    state.slots[1].trim_start = 4.0
    script = render_shell_script(state, Path("wall.mp4"))
    assert "Start trims" not in script
    assert "Wall fragments: 1=1.00s–5.00s" in script
    assert "=4.00s" not in script


def test_puzzle_script_reports_start_trims() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    state.slots[0].trim_start = 1.5
    script = render_shell_script(state, Path("out.mp4"))
    assert "# Start trims: 1=1.50s, 2=0.00s" in script


def test_resolved_ffmpeg_binary_is_used_for_render_and_still() -> None:
    state = filled_state(Layout.TWO_HORIZONTAL)
    args = build_ffmpeg_args(state, Path("out.mp4"), ffmpeg="/opt/bin/ffmpeg")
    still = build_still_args(state, Path("still.jpg"), ffmpeg="/opt/bin/ffmpeg")
    assert args[0] == "/opt/bin/ffmpeg"
    assert still[0] == "/opt/bin/ffmpeg"
    # A saved script stays portable: it must not bake in this machine's path.
    assert "ffmpeg" in render_shell_script(state, Path("out.mp4"))
