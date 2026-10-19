import pytest

from video_puzzle.layout import (
    Layout,
    canvas_size,
    cell_sizes,
    even,
    grid_output_size,
    is_pyramid,
    mosaic_frame_size,
    mosaic_pad_boxes,
    slot_count,
)


def test_even_strips_odd_bit() -> None:
    assert even(427) == 426
    assert even(428) == 428
    assert even(0) == 0


def test_known_canvas_sizes() -> None:
    assert canvas_size(480) == (848, 480)
    assert canvas_size(720) == (1280, 720)
    assert canvas_size(1080) == (1920, 1080)


def test_unknown_resolution_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported resolution"):
        canvas_size(1440)


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        (Layout.TWO_HORIZONTAL, 2),
        (Layout.TWO_VERTICAL, 2),
        (Layout.THREE_PYRAMID, 3),
        (Layout.THREE_PYRAMID_NARROW, 3),
        (Layout.FOUR_SQUARE, 4),
    ],
)
def test_slot_count(layout: Layout, expected: int) -> None:
    assert slot_count(layout) == expected


@pytest.mark.parametrize("height", [480, 720, 1080])
@pytest.mark.parametrize(
    "layout",
    [
        Layout.TWO_HORIZONTAL,
        Layout.TWO_VERTICAL,
        Layout.THREE_PYRAMID,
        Layout.THREE_PYRAMID_NARROW,
        Layout.FOUR_SQUARE,
    ],
)
def test_all_cell_sides_are_even(layout: Layout, height: int) -> None:
    width, canvas_h = canvas_size(height)
    for cell_w, cell_h in cell_sizes(layout, width, canvas_h):
        assert cell_w % 2 == 0
        assert cell_h % 2 == 0


def test_two_horizontal_cells_fill_width() -> None:
    cells = cell_sizes(Layout.TWO_HORIZONTAL, 1920, 1080)
    assert cells == [(960, 1080), (960, 1080)]
    assert mosaic_frame_size(Layout.TWO_HORIZONTAL, 1920, 1080) == (1920, 1080)


def test_two_vertical_cells_fill_height() -> None:
    cells = cell_sizes(Layout.TWO_VERTICAL, 1920, 1080)
    assert cells == [(1920, 540), (1920, 540)]
    assert mosaic_frame_size(Layout.TWO_VERTICAL, 1920, 1080) == (1920, 1080)


def test_pyramid_top_is_full_width() -> None:
    cells = cell_sizes(Layout.THREE_PYRAMID, 1920, 1080)
    assert cells[0] == (1920, 540)
    assert cells[1] == (960, 540)
    assert cells[2] == (960, 540)
    assert mosaic_frame_size(Layout.THREE_PYRAMID, 1920, 1080) == (1920, 1080)


def test_narrow_pyramid_top_is_half_width_then_padded() -> None:
    cells = cell_sizes(Layout.THREE_PYRAMID_NARROW, 1920, 1080)
    assert cells[0] == (960, 540)
    assert cells[1] == (960, 540)
    assert cells[2] == (960, 540)
    boxes = mosaic_pad_boxes(Layout.THREE_PYRAMID_NARROW, 1920, 1080)
    assert boxes[0] == (1920, 540)
    assert boxes[1] == (960, 540)
    assert mosaic_frame_size(Layout.THREE_PYRAMID_NARROW, 1920, 1080) == (1920, 1080)


def test_square_is_quarters() -> None:
    cells = cell_sizes(Layout.FOUR_SQUARE, 1920, 1080)
    assert cells == [(960, 540)] * 4
    assert mosaic_frame_size(Layout.FOUR_SQUARE, 1920, 1080) == (1920, 1080)


def test_480p_output_stays_even() -> None:
    width, height = canvas_size(480)
    out_w, out_h = mosaic_frame_size(Layout.FOUR_SQUARE, width, height)
    assert out_w % 2 == 0
    assert out_h % 2 == 0
    assert out_w == 848
    assert out_h == 480


@pytest.mark.parametrize("layout", list(Layout))
@pytest.mark.parametrize("height", [480, 720, 1080])
@pytest.mark.parametrize("gap", [0, 2, 6, 40])
def test_gap_pads_keep_stack_sides_matching(layout: Layout, height: int, gap: int) -> None:
    """hstack/vstack reject mismatched sides; padded boxes must stay consistent."""
    width, canvas_h = canvas_size(height)
    boxes = mosaic_pad_boxes(layout, width, canvas_h, gap)
    for box_w, box_h in boxes:
        assert box_w % 2 == 0
        assert box_h % 2 == 0
    if is_pyramid(layout):
        assert boxes[0][0] == boxes[1][0] + boxes[2][0]
        assert boxes[1][1] == boxes[2][1]
    if layout in {Layout.TWO_HORIZONTAL, Layout.FOUR_SQUARE}:
        assert boxes[0][1] == boxes[1][1]
    if layout in {Layout.TWO_VERTICAL, Layout.FOUR_SQUARE}:
        assert boxes[0][0] == boxes[1][0]


@pytest.mark.parametrize(
    ("layout", "gap", "expected"),
    [
        (Layout.TWO_HORIZONTAL, 0, (1920, 1080)),
        (Layout.TWO_HORIZONTAL, 6, (1926, 1080)),
        (Layout.TWO_VERTICAL, 6, (1920, 1086)),
        (Layout.THREE_PYRAMID, 6, (1926, 1086)),
        (Layout.THREE_PYRAMID_NARROW, 6, (1926, 1086)),
        (Layout.FOUR_SQUARE, 6, (1926, 1086)),
    ],
)
def test_frame_size_accounts_for_gap(layout: Layout, gap: int, expected: tuple[int, int]) -> None:
    assert mosaic_frame_size(layout, 1920, 1080, gap) == expected


def test_pyramid_gap_grows_frame_by_two_gaps_wide() -> None:
    """Bottom row is two padded cells, so width grows by 2*gap and height by 2*gap."""
    boxes = mosaic_pad_boxes(Layout.THREE_PYRAMID, 1920, 1080, 6)
    assert boxes == [(1932, 546), (966, 546), (966, 546)]
    assert mosaic_frame_size(Layout.THREE_PYRAMID, 1920, 1080, 6) == (1926, 1086)


def test_odd_gap_is_rounded_down_to_even() -> None:
    assert mosaic_pad_boxes(Layout.FOUR_SQUARE, 1920, 1080, 7) == mosaic_pad_boxes(
        Layout.FOUR_SQUARE, 1920, 1080, 6
    )


def test_wall_output_has_gaps_only_between_cells() -> None:
    # 3 columns of 640 px and two 6 px gaps; no outer border.
    assert grid_output_size(3, 2, 1920, 1080, 6) == (1932, 1086)
