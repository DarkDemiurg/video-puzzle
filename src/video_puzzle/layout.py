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


def pad_size(layout: Layout, index: int, canvas_width: int, canvas_height: int) -> tuple[int, int]:
    """Target pad box for a cell; equals the scale size except the narrow pyramid top."""
    scale_w, scale_h = cell_sizes(layout, canvas_width, canvas_height)[index]
    if layout is Layout.THREE_PYRAMID_NARROW and index == 0:
        return even(canvas_width), scale_h
    return scale_w, scale_h


def output_size(layout: Layout, canvas_width: int, canvas_height: int) -> tuple[int, int]:
    """Actual encoded frame size after stacking even-sized cells."""
    cells = cell_sizes(layout, canvas_width, canvas_height)
    if layout is Layout.TWO_HORIZONTAL:
        w0, h0 = cells[0]
        w1, h1 = cells[1]
        return w0 + w1, max(h0, h1)
    if layout is Layout.TWO_VERTICAL:
        w0, h0 = cells[0]
        w1, h1 = cells[1]
        return max(w0, w1), h0 + h1
    if layout is Layout.THREE_PYRAMID:
        top_w, top_h = cells[0]
        left_w, left_h = cells[1]
        right_w, right_h = cells[2]
        return max(top_w, left_w + right_w), top_h + max(left_h, right_h)
    if layout is Layout.THREE_PYRAMID_NARROW:
        _top_w, top_h = cells[0]
        left_w, left_h = cells[1]
        right_w, right_h = cells[2]
        pad_w, _ = pad_size(layout, 0, canvas_width, canvas_height)
        return max(pad_w, left_w + right_w), top_h + max(left_h, right_h)
    if layout is Layout.FOUR_SQUARE:
        w, h = cells[0]
        return w * 2, h * 2
    raise ValueError(f"Unknown layout: {layout}")


MAX_GRID = 8
MAX_SLOTS = MAX_GRID * MAX_GRID


class AppMode(StrEnum):
    PUZZLE = "puzzle"
    WALL = "wall"


def grid_cell_size(cols: int, rows: int, canvas_width: int, canvas_height: int) -> tuple[int, int]:
    if cols < 1 or rows < 1:
        raise ValueError("Grid must be at least 1×1")
    if cols > MAX_GRID or rows > MAX_GRID:
        raise ValueError(f"Grid cannot exceed {MAX_GRID}×{MAX_GRID}")
    return even(canvas_width // cols), even(canvas_height // rows)


def grid_output_size(
    cols: int, rows: int, canvas_width: int, canvas_height: int
) -> tuple[int, int]:
    cell_w, cell_h = grid_cell_size(cols, rows, canvas_width, canvas_height)
    return cell_w * cols, cell_h * rows


def xstack_layout(rows: int, cols: int) -> str:
    """ffmpeg xstack layout with equal cells, row-major order."""
    parts: list[str] = []
    for row in range(rows):
        for col in range(cols):
            x = "0" if col == 0 else "+".join(f"w{c}" for c in range(col))
            y = "0" if row == 0 else "+".join(f"h{r * cols}" for r in range(row))
            parts.append(f"{x}_{y}")
    return "|".join(parts)
