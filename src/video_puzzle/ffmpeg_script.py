from __future__ import annotations

import shlex
from pathlib import Path

from video_puzzle.layout import (
    AppMode,
    Layout,
    canvas_size,
    cell_sizes,
    grid_cell_size,
    grid_output_size,
    output_size,
    pad_size,
    xstack_layout,
)
from video_puzzle.state import AppState

DEFAULT_FPS = 30
DEFAULT_CRF = 18


class IncompleteStateError(ValueError):
    """Raised when a mosaic script is requested before every active slot is filled."""


def _time_trim(start: float, end: float | None, *, audio: bool) -> list[str]:
    trim = "atrim" if audio else "trim"
    reset = "asetpts=PTS-STARTPTS" if audio else "setpts=PTS-STARTPTS"
    has_start = start > 0.0005
    has_end = end is not None and end > start + 0.0005
    if not has_start and not has_end:
        return []
    if has_start and has_end:
        return [f"{trim}=start={start:.4f}:end={end:.4f}", reset]
    if has_end:
        return [f"{trim}=end={end:.4f}", reset]
    return [f"{trim}=start={start:.4f}", reset]


def _prep_filter(
    index: int,
    scale_w: int,
    scale_h: int,
    fps: int,
    *,
    start: float,
    end: float | None,
    pad_w: int,
    pad_h: int,
) -> str:
    chain = [
        *_time_trim(start, end, audio=False),
        f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease",
        f"pad={pad_w}:{pad_h}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        f"fps={fps}",
    ]
    return f"[{index}:v]" + ",".join(chain) + f"[v{index}]"


def _audio_filter(index: int, start: float, end: float | None) -> str:
    chain = [*_time_trim(start, end, audio=True), "aresample=48000"]
    return f"[{index}:a]" + ",".join(chain) + "[outa]"


def _stack_filters(layout: Layout) -> list[str]:
    if layout is Layout.TWO_HORIZONTAL:
        return ["[v0][v1]hstack=inputs=2[outv]"]
    if layout is Layout.TWO_VERTICAL:
        return ["[v0][v1]vstack=inputs=2[outv]"]
    if layout in {Layout.THREE_PYRAMID, Layout.THREE_PYRAMID_NARROW}:
        return [
            "[v1][v2]hstack=inputs=2[bottom]",
            "[v0][bottom]vstack=inputs=2[outv]",
        ]
    if layout is Layout.FOUR_SQUARE:
        return [
            "[v0][v1]hstack=inputs=2[top]",
            "[v2][v3]hstack=inputs=2[bottom]",
            "[top][bottom]vstack=inputs=2[outv]",
        ]
    raise ValueError(f"Unknown layout: {layout}")


def _wall_stack_filters(rows: int, cols: int) -> list[str]:
    count = rows * cols
    labels = "".join(f"[v{i}]" for i in range(count))
    if count == 1:
        return ["[v0]null[outv]"]
    return [f"{labels}xstack=inputs={count}:layout={xstack_layout(rows, cols)}[outv]"]


def _mosaic_cells(state: AppState) -> list[tuple[int, int]]:
    width, height = canvas_size(state.resolution)
    if state.mode is AppMode.WALL:
        cell = grid_cell_size(state.wall_cols, state.wall_rows, width, height)
        return [cell] * state.active_count
    return cell_sizes(state.layout, width, height)


def _mosaic_pad(state: AppState, index: int, scale_w: int, scale_h: int) -> tuple[int, int]:
    if state.mode is AppMode.WALL:
        return scale_w, scale_h
    width, height = canvas_size(state.resolution)
    return pad_size(state.layout, index, width, height)


def _mosaic_output_size(state: AppState) -> tuple[int, int]:
    width, height = canvas_size(state.resolution)
    if state.mode is AppMode.WALL:
        return grid_output_size(state.wall_cols, state.wall_rows, width, height)
    return output_size(state.layout, width, height)


def build_filter_complex(state: AppState, *, fps: int = DEFAULT_FPS) -> str:
    cells = _mosaic_cells(state)
    count = state.active_count
    if len(cells) != count:
        raise ValueError("Layout cell count does not match active slot count")
    prep = []
    for index, (scale_w, scale_h) in enumerate(cells):
        pad_w, pad_h = _mosaic_pad(state, index, scale_w, scale_h)
        start, end = state.source_window(index)
        prep.append(
            _prep_filter(
                index, scale_w, scale_h, fps, start=start, end=end, pad_w=pad_w, pad_h=pad_h
            )
        )
    if state.mode is AppMode.WALL:
        stack = _wall_stack_filters(state.wall_rows, state.wall_cols)
    else:
        stack = _stack_filters(state.layout)
    parts = [*prep, *stack]
    audio_index = state.audio_input_index()
    if audio_index is not None:
        audio_start, audio_end = state.source_window(audio_index)
        parts.append(_audio_filter(audio_index, audio_start, audio_end))
    return ";".join(parts)


def build_ffmpeg_args(
    state: AppState,
    output: Path,
    *,
    fps: int = DEFAULT_FPS,
    crf: int = DEFAULT_CRF,
    overwrite: bool = False,
    progress: bool = False,
) -> list[str]:
    if not state.is_complete():
        missing = ", ".join(str(i + 1) for i in state.missing_slot_indexes())
        raise IncompleteStateError(f"Fill all slots before generating a script (empty: {missing})")

    args: list[str] = ["ffmpeg"]
    if overwrite:
        args.append("-y")
    args.append("-hide_banner")
    if progress:
        args.extend(["-progress", "pipe:1", "-nostats"])
    for slot in state.active_slots():
        assert slot.path is not None
        args.extend(["-i", str(slot.path)])
    args.extend(["-filter_complex", build_filter_complex(state, fps=fps), "-map", "[outv]"])
    audio_index = state.audio_input_index()
    if audio_index is None:
        args.append("-an")
    else:
        args.extend(["-map", "[outa]", "-c:a", "aac", "-ac", "2", "-b:a", "192k"])
    args.extend(
        [
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(output),
        ]
    )
    return args


def render_shell_script(state: AppState, output: Path, *, fps: int = DEFAULT_FPS) -> str:
    args = build_ffmpeg_args(state, output, fps=fps)
    canvas_w, canvas_h = canvas_size(state.resolution)
    out_w, out_h = _mosaic_output_size(state)
    audio = state.audio_input_index()
    audio_note = (
        f"audio from input {audio_index_label(audio)}" if audio is not None else "audio omitted"
    )
    if state.mode is AppMode.WALL:
        layout_note = f"wall {state.wall_rows}x{state.wall_cols}"
        range_note = (
            f"# Wall duration = shortest fragment ({state.wall_output_duration() or 0:.2f}s)"
        )
    else:
        layout_note = f"layout={state.layout}"
        range_note = (
            f"# Output range: {state.resolved_range_start():.2f}s–{_range_end_label(state)}"
        )
    trims = ", ".join(
        f"{index + 1}={state.slots[index].trim_start:.2f}s" for index in range(state.active_count)
    )
    header = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"# Video Puzzle — {layout_note}  canvas={canvas_w}x{canvas_h}  output={out_w}x{out_h}",
        f"# Duration follows the shortest aligned input (-shortest). {audio_note}.",
        f"# Start trims: {trims}",
        range_note,
    ]
    return "\n".join([*header, shlex.join(args), ""])


def audio_index_label(index: int) -> str:
    return str(index + 1)


def _range_end_label(state: AppState) -> str:
    end = state.resolved_range_end()
    if end is None:
        return "end"
    return f"{end:.2f}s"
