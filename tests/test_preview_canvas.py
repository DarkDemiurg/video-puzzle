from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from video_puzzle.layout import Layout, even
from video_puzzle.project import load_project, save_project
from video_puzzle.state import AppState
from video_puzzle.ui.main_window import MainWindow
from video_puzzle.ui.preview_canvas import PreviewCanvas

_APP = None


def _app() -> QApplication:
    global _APP
    instance = QApplication.instance()
    if instance is None:
        _APP = QApplication([])
        return _APP
    return instance


def test_layout_changes_keep_slot_widgets_alive() -> None:
    app = _app()
    canvas = PreviewCanvas()
    canvas.show()
    canvas.apply_layout(Layout.TWO_HORIZONTAL)
    canvas.apply_layout(Layout.THREE_PYRAMID)
    canvas.apply_layout(Layout.FOUR_SQUARE)
    canvas.apply_grid(3, 4)
    canvas.apply_layout(Layout.TWO_VERTICAL)
    canvas.apply_layout(Layout.FOUR_SQUARE)
    app.processEvents()
    assert all(isValid(slot) for slot in canvas.slots)
    visible = [slot for slot in canvas.slots if not slot.isHidden()]
    assert len(visible) == 4
    assert "Drop a video" in canvas.slots[0].preview.text()


def test_main_window_scheme_and_mode_keep_placeholders() -> None:
    app = _app()
    window = MainWindow()
    window.show()
    window.radio_2.setChecked(True)
    app.processEvents()
    assert window.two_wrap.isVisible()
    assert window.pyramid_wrap.isHidden()
    window.radio_3.setChecked(True)
    app.processEvents()
    assert window.two_wrap.isHidden()
    assert window.pyramid_wrap.isVisible()
    window.radio_4.setChecked(True)
    app.processEvents()
    assert window.two_wrap.isHidden()
    assert window.pyramid_wrap.isHidden()
    window.radio_wall.setChecked(True)
    app.processEvents()
    window.rows_spin.setValue(3)
    window.cols_spin.setValue(3)
    app.processEvents()
    assert not window.auto_frag_btn.isHidden()
    assert not window.auto_frag_btn.isEnabled()
    window.radio_puzzle.setChecked(True)
    app.processEvents()
    slots = window.canvas.slots
    assert all(isValid(slot) for slot in slots)
    visible = [slot for slot in slots if not slot.isHidden()]
    assert len(visible) == 4
    assert "Drop a video" in visible[0].preview.text()


def test_apply_loaded_project_restores_scheme(tmp_path: Path) -> None:
    app = _app()
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    clip_a.write_bytes(b"")
    clip_b.write_bytes(b"")
    state = AppState(layout=Layout.TWO_VERTICAL, resolution=720)
    state.set_slot(0, clip_a)
    state.set_slot(1, clip_b)
    dest = tmp_path / "job.vproj"
    save_project(state, dest)

    window = MainWindow()
    window.show()
    window.state = load_project(dest)
    window._apply_state_to_widgets()
    app.processEvents()
    assert window.radio_2.isChecked()
    assert window.radio_v.isChecked()
    assert window.two_wrap.isVisible()
    assert window.pyramid_wrap.isHidden()
    assert window.res_buttons[720].isChecked()
    assert window.res_buttons[1080].text() == "1080p"
    window.close()


def test_main_window_minimum_height_allows_maximize() -> None:
    app = _app()
    window = MainWindow()
    window.show()
    app.processEvents()
    assert window.minimumSizeHint().height() < 780
    assert window.maximumSize().height() > 4000
    window.close()


def test_sidebar_width_can_be_dragged() -> None:
    app = _app()
    window = MainWindow()
    window.show()
    app.processEvents()
    assert window.splitter.count() == 2
    window.splitter.setSizes([400, 700])
    app.processEvents()
    assert window.splitter.sizes()[0] >= 240
    window.close()


def _inner_spacing(canvas) -> int:
    layout = canvas._inner.layout()
    assert layout is not None
    return layout.spacing()


@pytest.mark.parametrize("gap", [0, 2, 4, 6, 10, 40])
def test_preview_spacing_follows_gap_without_a_hidden_minimum(gap: int) -> None:
    """Раньше превью рисовало минимум 8 px: при gap 0..6 оно врало про реальный рендер."""
    app = _app()
    window = MainWindow()
    window.show()
    window.gap_spin.setValue(gap)
    app.processEvents()
    assert _inner_spacing(window.canvas) == even(window.state.cell_gap)
    assert _inner_spacing(window.canvas) == even(gap)
    window.close()


def test_wall_preview_spacing_matches_gap() -> None:
    app = _app()
    window = MainWindow()
    window.show()
    window.radio_wall.setChecked(True)
    window.rows_spin.setValue(2)
    window.cols_spin.setValue(2)
    window.gap_spin.setValue(0)
    app.processEvents()
    assert _inner_spacing(window.canvas) == 0
    window.gap_spin.setValue(6)
    app.processEvents()
    assert _inner_spacing(window.canvas) == 6
    window.close()


def test_preview_spacing_matches_ffmpeg_gap() -> None:
    """UI и ffmpeg должны считать зазор одинаково, иначе превью ≠ результат."""
    app = _app()
    window = MainWindow()
    window.show()
    for gap in (0, 6, 12, 40):
        window.gap_spin.setValue(gap)
        app.processEvents()
        assert _inner_spacing(window.canvas) == even(window.state.cell_gap)
    window.close()


def test_preview_has_no_outer_margin_around_the_gap() -> None:
    """ffmpeg crops the trailing gap, so the preview must not add an outer border."""
    app = _app()
    window = MainWindow()
    window.show()
    window.gap_spin.setValue(6)
    app.processEvents()
    layout = window.canvas._inner.layout()
    assert layout is not None
    margins = layout.contentsMargins()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 0, 0, 0)
    window.close()
