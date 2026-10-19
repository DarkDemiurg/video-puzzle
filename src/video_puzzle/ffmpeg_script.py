from __future__ import annotations

import shlex
from pathlib import Path

from video_puzzle.encode import ENCODE_PRESETS, video_encoder_args
from video_puzzle.encoders import resolve_encoder
from video_puzzle.i18n import tr
from video_puzzle.layout import (
    AppMode,
    Layout,
    canvas_size,
    cell_sizes,
    even,
    grid_cell_boxes,
    grid_output_size,
    grid_pad_boxes,
    mosaic_frame_size,
    mosaic_pad_boxes,
    xstack_layout,
)
from video_puzzle.state import AppState, Slot

DEFAULT_FPS = 30


class IncompleteStateError(ValueError):
    """Raised when a mosaic script is requested before every active slot is filled."""


def _require_ready(state: AppState, *, what: str) -> None:
    """Refuse to build an export for an incomplete scheme.

    A wall whose cells have no I/O marks used to pass this check: ffmpeg then took
    every file from frame one and ``-shortest`` stretched the mosaic to the length
    of the longest full clip instead of the intended fragment.
    """
    missing_files = state.missing_slot_indexes()
    if missing_files:
        empty = ", ".join(str(i + 1) for i in missing_files)
        raise IncompleteStateError(
            tr(
                "Fill all slots before generating {what} (empty: {empty})",
                what=what,
                empty=empty,
            )
        )
    missing_fragments = state.missing_fragment_indexes()
    if missing_fragments:
        cells = ", ".join(str(i + 1) for i in missing_fragments)
        raise IncompleteStateError(
            tr(
                "Mark a fragment (I/O) in every wall cell before generating {what} "
                "(unset: {cells})",
                what=what,
                cells=cells,
            )
        )


def _seek_args(start: float, end: float | None) -> list[str]:
    """Fast input window so ffmpeg does not decode from the start of the file."""
    args: list[str] = []
    if start > 0.0005:
        args.extend(["-ss", f"{start:.4f}"])
    if end is not None and end > start + 0.0005:
        args.extend(["-t", f"{max(0.04, end - start):.4f}"])
    return args


def _append_inputs(args: list[str], state: AppState) -> None:
    for index, slot in enumerate(state.active_slots()):
        assert slot.path is not None
        start, end = state.source_window(index)
        args.extend([*_seek_args(start, end), "-i", str(slot.path)])


def _rotate_filters(rotation: int) -> list[str]:
    rot = rotation % 360
    if rot == 90:
        return ["transpose=1"]
    if rot == 180:
        return ["hflip", "vflip"]
    if rot == 270:
        return ["transpose=2"]
    return []


def _crop_filters(crop: tuple[float, float, float, float] | None) -> list[str]:
    if crop is None:
        return []
    x, y, width, height = crop
    if width < 0.02 or height < 0.02:
        return []
    x = min(max(0.0, x), 0.98)
    y = min(max(0.0, y), 0.98)
    width = min(max(0.02, width), 1.0 - x)
    height = min(max(0.02, height), 1.0 - y)
    return [f"crop=iw*{width:.4f}:ih*{height:.4f}:iw*{x:.4f}:ih*{y:.4f}"]


def _prep_filter(
    index: int,
    slot: Slot,
    scale_w: int,
    scale_h: int,
    fps: float,
    *,
    pad_w: int,
    pad_h: int,
) -> str:
    fps_label = str(int(round(fps))) if abs(fps - round(fps)) < 0.001 else f"{fps:.3f}"
    chain = [
        *_rotate_filters(slot.rotation),
        *_crop_filters(slot.crop),
        f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease",
        f"pad={pad_w}:{pad_h}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        f"fps={fps_label}",
    ]
    return f"[{index}:v]" + ",".join(chain) + f"[v{index}]"


def _audio_filter(index: int, *, normalize: bool) -> str:
    chain = ["aresample=48000"]
    if normalize:
        chain.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return f"[{index}:a]" + ",".join(chain) + "[outa]"


def _stack_filters(layout: Layout, *, final: str = "outv") -> list[str]:
    if layout is Layout.TWO_HORIZONTAL:
        return [f"[v0][v1]hstack=inputs=2[{final}]"]
    if layout is Layout.TWO_VERTICAL:
        return [f"[v0][v1]vstack=inputs=2[{final}]"]
    if layout in {Layout.THREE_PYRAMID, Layout.THREE_PYRAMID_NARROW}:
        return [
            "[v1][v2]hstack=inputs=2[bottom]",
            f"[v0][bottom]vstack=inputs=2[{final}]",
        ]
    if layout is Layout.FOUR_SQUARE:
        return [
            "[v0][v1]hstack=inputs=2[top]",
            "[v2][v3]hstack=inputs=2[bottom]",
            f"[top][bottom]vstack=inputs=2[{final}]",
        ]
    raise ValueError(f"Unknown layout: {layout}")


def _wall_stack_filters(rows: int, cols: int, *, final: str = "outv") -> list[str]:
    count = rows * cols
    labels = "".join(f"[v{i}]" for i in range(count))
    if count == 1:
        return [f"[v0]null[{final}]"]
    return [f"{labels}xstack=inputs={count}:layout={xstack_layout(rows, cols)}[{final}]"]


def _gap(state: AppState) -> int:
    return even(state.cell_gap)


def _mosaic_cells(state: AppState) -> list[tuple[int, int]]:
    width, height = canvas_size(state.resolution)
    if state.mode is AppMode.WALL:
        return grid_cell_boxes(state.wall_cols, state.wall_rows, width, height)
    return cell_sizes(state.layout, width, height)


def _mosaic_pads(state: AppState) -> list[tuple[int, int]]:
    width, height = canvas_size(state.resolution)
    gap = _gap(state)
    if state.mode is AppMode.WALL:
        return grid_pad_boxes(state.wall_cols, state.wall_rows, width, height, gap)
    return mosaic_pad_boxes(state.layout, width, height, gap)


def mosaic_output_size(state: AppState) -> tuple[int, int]:
    width, height = canvas_size(state.resolution)
    gap = _gap(state)
    if state.mode is AppMode.WALL:
        return grid_output_size(state.wall_cols, state.wall_rows, width, height, gap)
    return mosaic_frame_size(state.layout, width, height, gap)


def build_filter_complex(state: AppState, *, fps: float | None = None) -> str:
    cells = _mosaic_cells(state)
    pads = _mosaic_pads(state)
    count = state.active_count
    if len(cells) != count or len(pads) != count:
        raise ValueError("Layout cell count does not match active slot count")
    rate = state.output_fps() if fps is None else fps
    prep = []
    for index, (scale_w, scale_h) in enumerate(cells):
        pad_w, pad_h = pads[index]
        prep.append(
            _prep_filter(
                index,
                state.slots[index],
                scale_w,
                scale_h,
                rate,
                pad_w=pad_w,
                pad_h=pad_h,
            )
        )
    gap = _gap(state)
    stacked = "stacked" if gap else "outv"
    if state.mode is AppMode.WALL:
        stack = _wall_stack_filters(state.wall_rows, state.wall_cols, final=stacked)
    else:
        stack = _stack_filters(state.layout, final=stacked)
    parts = [*prep, *stack]
    if gap:
        # Padding puts a gap on the right/bottom of every cell; drop the trailing strip
        # so only the gaps *between* cells remain.
        parts.append(f"[stacked]crop=iw-{gap}:ih-{gap}:0:0[outv]")
    audio_index = state.audio_input_index()
    if audio_index is not None:
        parts.append(_audio_filter(audio_index, normalize=state.normalize_audio))
    return ";".join(parts)


def build_ffmpeg_args(
    state: AppState,
    output: Path,
    *,
    fps: float | None = None,
    overwrite: bool = False,
    progress: bool = False,
    available_encoders: set[str] | None = None,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    _require_ready(state, what=tr("a script"))
    settings = ENCODE_PRESETS[state.quality]
    rate = state.output_fps() if fps is None else fps
    kind = resolve_encoder(state.encoder, available_encoders or {"libx264"})
    args: list[str] = [ffmpeg]
    if overwrite:
        args.append("-y")
    args.append("-hide_banner")
    if progress:
        args.extend(["-progress", "pipe:1", "-nostats"])
    _append_inputs(args, state)
    args.extend(["-filter_complex", build_filter_complex(state, fps=rate), "-map", "[outv]"])
    audio_index = state.audio_input_index()
    if audio_index is None:
        args.append("-an")
    else:
        args.extend(["-map", "[outa]", "-c:a", "aac", "-ac", "2", "-b:a", settings.audio_bitrate])
    args.extend([*video_encoder_args(kind, state.quality), "-shortest", str(output)])
    return args


def build_still_args(state: AppState, dest: Path, *, ffmpeg: str = "ffmpeg") -> list[str]:
    _require_ready(state, what=tr("a preview"))
    args: list[str] = [ffmpeg, "-y", "-hide_banner"]
    _append_inputs(args, state)
    args.extend(
        [
            "-filter_complex",
            build_filter_complex(state),
            "-map",
            "[outv]",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(dest),
        ]
    )
    return args


def render_shell_script(
    state: AppState,
    output: Path,
    *,
    fps: float | None = None,
    available_encoders: set[str] | None = None,
) -> str:
    # -y: a saved script is meant to be re-run after tweaking the project, and an
    # interactive "File exists. Overwrite?" prompt would hang any non-terminal launch.
    args = build_ffmpeg_args(
        state, output, fps=fps, overwrite=True, available_encoders=available_encoders
    )
    canvas_w, canvas_h = canvas_size(state.resolution)
    out_w, out_h = mosaic_output_size(state)
    audio = state.audio_input_index()
    audio_note = (
        f"audio from input {audio_index_label(audio)}" if audio is not None else "audio omitted"
    )
    if state.mode is AppMode.WALL:
        layout_note = f"wall {state.wall_rows}x{state.wall_cols}"
        range_note = (
            f"# Wall duration = shortest fragment ({state.wall_output_duration() or 0:.2f}s)"
        )
        # A wall takes its window from the I/O marks; ``trim_start`` is a puzzle-only
        # offset. Printing it here advertised a "-ss" that the command does not contain.
        fragments = ", ".join(
            f"{index + 1}={state.source_window(index)[0]:.2f}s"
            f"–{(state.source_window(index)[1] or 0.0):.2f}s"
            for index in range(state.active_count)
        )
        offsets_note = f"# Wall fragments: {fragments}"
    else:
        layout_note = f"layout={state.layout}"
        range_note = (
            f"# Output range: {state.resolved_range_start():.2f}s–{_range_end_label(state)}"
        )
        trims = ", ".join(
            f"{index + 1}={state.slots[index].trim_start:.2f}s"
            for index in range(state.active_count)
        )
        offsets_note = f"# Start trims: {trims}"
    settings = ENCODE_PRESETS[state.quality]
    kind = resolve_encoder(state.encoder, available_encoders or {"libx264"})
    header = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"# Video Puzzle — {layout_note}  canvas={canvas_w}x{canvas_h}  output={out_w}x{out_h}",
        f"# Duration follows the shortest aligned input (-shortest). {audio_note}.",
        f"# Quality: {state.quality}  encoder={kind}  crf={settings.crf}"
        f"  fps={state.output_fps():.3f}",
        offsets_note,
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
