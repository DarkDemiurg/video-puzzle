from enum import StrEnum


class Layout(StrEnum):
    TWO_HORIZONTAL = "2h"
    TWO_VERTICAL = "2v"
    THREE_PYRAMID = "3pyramid"
    THREE_PYRAMID_NARROW = "3pyramid_narrow"
    FOUR_SQUARE = "4square"


SLOT_COUNT: dict[Layout, int] = {
    Layout.TWO_HORIZONTAL: 2,
    Layout.TWO_VERTICAL: 2,
    Layout.THREE_PYRAMID: 3,
    Layout.THREE_PYRAMID_NARROW: 3,
    Layout.FOUR_SQUARE: 4,
}

# 480p uses 848x480 so every mosaic split stays even (yuv420 / libx264).
RESOLUTIONS: dict[int, tuple[int, int]] = {
    480: (848, 480),
    720: (1280, 720),
    1080: (1920, 1080),
}

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})


def even(value: int) -> int:
    return value - (value % 2)


def canvas_size(height: int) -> tuple[int, int]:
    try:
        return RESOLUTIONS[height]
    except KeyError as exc:
        allowed = ", ".join(str(h) for h in RESOLUTIONS)
        raise ValueError(f"Unsupported resolution {height}; expected one of: {allowed}") from exc


def slot_count(layout: Layout) -> int:
    return SLOT_COUNT[layout]


def is_pyramid(layout: Layout) -> bool:
    return layout in {Layout.THREE_PYRAMID, Layout.THREE_PYRAMID_NARROW}


def cell_sizes(layout: Layout, canvas_width: int, canvas_height: int) -> list[tuple[int, int]]:
    """Pixel size of each mosaic cell before optional centering pad, in slot order."""
    half_w = even(canvas_width // 2)
    half_h = even(canvas_height // 2)
    full_w = even(canvas_width)
    full_h = even(canvas_height)

    if layout is Layout.TWO_HORIZONTAL:
        return [(half_w, full_h), (half_w, full_h)]
    if layout is Layout.TWO_VERTICAL:
        return [(full_w, half_h), (full_w, half_h)]
    if layout is Layout.THREE_PYRAMID:
        return [(full_w, half_h), (half_w, half_h), (half_w, half_h)]
    if layout is Layout.THREE_PYRAMID_NARROW:
        return [(half_w, half_h), (half_w, half_h), (half_w, half_h)]
    if layout is Layout.FOUR_SQUARE:
        return [(half_w, half_h)] * 4
    raise ValueError(f"Unknown layout: {layout}")


def mosaic_pad_boxes(
    layout: Layout, canvas_width: int, canvas_height: int, gap: int = 0
) -> list[tuple[int, int]]:
    """Padded box of every cell (scale target plus the surrounding gap), slot order.

    ``hstack``/``vstack`` refuse inputs of different sizes, so a pyramid top has to span
    the *sum* of the two padded bottom boxes. Adding ``gap`` to each cell on its own makes
    the bottom row ``canvas + 2 * gap`` wide while the top stays ``canvas + gap``, and
    ffmpeg fails the whole graph with "Input 1 width ... does not match input 0 width ...".
    """
    gap = even(gap)
    boxes = [(w + gap, h + gap) for w, h in cell_sizes(layout, canvas_width, canvas_height)]
    if is_pyramid(layout):
        boxes[0] = (boxes[1][0] + boxes[2][0], boxes[0][1])
    return boxes


def mosaic_frame_size(
    layout: Layout, canvas_width: int, canvas_height: int, gap: int = 0
) -> tuple[int, int]:
    """Frame size ffmpeg produces after the trailing gap is cropped away.

    Padding every cell adds a gap on its right/bottom side. Stacking therefore
    produces ``n * (cell + gap)``, one gap wider than the intended
    ``n * cell + (n - 1) * gap``; the filter graph crops the trailing strip so the
    preview and the exported file show the same outer edge.
    """
    gap = even(gap)
    boxes = mosaic_pad_boxes(layout, canvas_width, canvas_height, gap)
    if layout is Layout.TWO_HORIZONTAL:
        width, height = boxes[0][0] + boxes[1][0], max(boxes[0][1], boxes[1][1])
    elif layout is Layout.TWO_VERTICAL:
        width, height = max(boxes[0][0], boxes[1][0]), boxes[0][1] + boxes[1][1]
    elif is_pyramid(layout):
        width, height = (
            boxes[1][0] + boxes[2][0],
            boxes[0][1] + max(boxes[1][1], boxes[2][1]),
        )
    elif layout is Layout.FOUR_SQUARE:
        width, height = boxes[0][0] + boxes[1][0], boxes[0][1] + boxes[2][1]
    else:
        raise ValueError(f"Unknown layout: {layout}")
    return width - gap, height - gap


MAX_GRID = 8
MAX_SLOTS = MAX_GRID * MAX_GRID


class AppMode(StrEnum):
    PUZZLE = "puzzle"
    WALL = "wall"


def _check_grid(cols: int, rows: int) -> None:
    if cols < 1 or rows < 1:
        raise ValueError("Grid must be at least 1×1")
    if cols > MAX_GRID or rows > MAX_GRID:
        raise ValueError(f"Grid cannot exceed {MAX_GRID}×{MAX_GRID}")


def grid_cell_size(cols: int, rows: int, canvas_width: int, canvas_height: int) -> tuple[int, int]:
    """Scale target of one wall cell; every cell is identical, so xstack stays happy."""
    _check_grid(cols, rows)
    return even(canvas_width // cols), even(canvas_height // rows)


def grid_cell_boxes(
    cols: int, rows: int, canvas_width: int, canvas_height: int
) -> list[tuple[int, int]]:
    """Scale target of every wall cell, row-major (matches xstack input order)."""
    return [grid_cell_size(cols, rows, canvas_width, canvas_height)] * (cols * rows)


def grid_pad_boxes(
    cols: int, rows: int, canvas_width: int, canvas_height: int, gap: int = 0
) -> list[tuple[int, int]]:
    """Padded box of every wall cell: the scale target plus the surrounding gap."""
    _check_grid(cols, rows)
    gap = even(gap)
    cell_w, cell_h = grid_cell_size(cols, rows, canvas_width, canvas_height)
    return [(cell_w + gap, cell_h + gap)] * (cols * rows)


def grid_output_size(
    cols: int, rows: int, canvas_width: int, canvas_height: int, gap: int = 0
) -> tuple[int, int]:
    _check_grid(cols, rows)
    gap = even(gap)
    cell_w, cell_h = grid_cell_size(cols, rows, canvas_width, canvas_height)
    # Gaps only sit between cells; the filter graph crops the trailing strip.
    return (cell_w + gap) * cols - gap, (cell_h + gap) * rows - gap


def xstack_layout(rows: int, cols: int) -> str:
    """ffmpeg xstack layout with equal cells, row-major order."""
    _check_grid(cols, rows)
    parts: list[str] = []
    for row in range(rows):
        for col in range(cols):
            x = "0" if col == 0 else "+".join(f"w{c}" for c in range(col))
            y = "0" if row == 0 else "+".join(f"h{r * cols}" for r in range(row))
            parts.append(f"{x}_{y}")
    return "|".join(parts)
