from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from video_puzzle.layout import Layout
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
    assert "Перетащите видео" in canvas.slots[0].preview.text()


def test_main_window_scheme_and_mode_keep_placeholders() -> None:
    app = _app()
    window = MainWindow()
    window.show()
    window.radio_2.setChecked(True)
    app.processEvents()
    window.radio_3.setChecked(True)
    app.processEvents()
    window.radio_4.setChecked(True)
    app.processEvents()
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
    assert "Перетащите видео" in visible[0].preview.text()
