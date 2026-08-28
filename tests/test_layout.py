import pytest

from video_puzzle.layout import (
    Layout,
    canvas_size,
    cell_sizes,
    even,
    output_size,
    pad_size,
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
    assert output_size(Layout.TWO_HORIZONTAL, 1920, 1080) == (1920, 1080)


def test_two_vertical_cells_fill_height() -> None:
    cells = cell_sizes(Layout.TWO_VERTICAL, 1920, 1080)
    assert cells == [(1920, 540), (1920, 540)]
    assert output_size(Layout.TWO_VERTICAL, 1920, 1080) == (1920, 1080)


def test_pyramid_top_is_full_width() -> None:
    cells = cell_sizes(Layout.THREE_PYRAMID, 1920, 1080)
    assert cells[0] == (1920, 540)
    assert cells[1] == (960, 540)
    assert cells[2] == (960, 540)
    assert output_size(Layout.THREE_PYRAMID, 1920, 1080) == (1920, 1080)


def test_narrow_pyramid_top_is_half_width_then_padded() -> None:
    cells = cell_sizes(Layout.THREE_PYRAMID_NARROW, 1920, 1080)
    assert cells[0] == (960, 540)
    assert cells[1] == (960, 540)
    assert cells[2] == (960, 540)
    assert pad_size(Layout.THREE_PYRAMID_NARROW, 0, 1920, 1080) == (1920, 540)
    assert pad_size(Layout.THREE_PYRAMID_NARROW, 1, 1920, 1080) == (960, 540)
    assert output_size(Layout.THREE_PYRAMID_NARROW, 1920, 1080) == (1920, 1080)


def test_square_is_quarters() -> None:
    cells = cell_sizes(Layout.FOUR_SQUARE, 1920, 1080)
    assert cells == [(960, 540)] * 4
    assert output_size(Layout.FOUR_SQUARE, 1920, 1080) == (1920, 1080)


def test_480p_output_stays_even() -> None:
    width, height = canvas_size(480)
    out_w, out_h = output_size(Layout.FOUR_SQUARE, width, height)
    assert out_w % 2 == 0
    assert out_h % 2 == 0
    assert out_w == 848
    assert out_h == 480
