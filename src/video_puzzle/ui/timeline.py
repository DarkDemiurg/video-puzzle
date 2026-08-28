from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QStyle, QStyleOptionSlider, QWidget

from video_puzzle.progress import format_timecode


class JumpSlider(QSlider):
    """QSlider that jumps to the click position instead of paging."""

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderHandle,
                self,
            )
            if not handle.contains(event.position().toPoint()):
                groove = self.style().subControlRect(
                    QStyle.ComplexControl.CC_Slider,
                    option,
                    QStyle.SubControl.SC_SliderGroove,
                    self,
                )
                span = (
                    groove.width()
                    if self.orientation() == Qt.Orientation.Horizontal
                    else groove.height()
                )
                pos = (
                    event.position().x() - groove.x()
                    if self.orientation() == Qt.Orientation.Horizontal
                    else event.position().y() - groove.y()
                )
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), int(pos), span
                )
                self.setValue(value)
        super().mousePressEvent(event)


class TimelineBar(QWidget):
    position_chosen = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("timelineBar")
        self._duration = 0.0
        self._range: tuple[float | None, float | None] = (None, None)
        self.slider = JumpSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("timeline")
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(25)
        self.label = QLabel("—")
        self.label.setObjectName("timecode")
        self.slider.valueChanged.connect(self._on_value)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)

    def set_duration(self, duration: float | None) -> None:
        enabled = duration is not None and duration > 0.04
        self._duration = duration or 0.0
        self.slider.setEnabled(enabled)
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(0, int(self._duration * 100)))
        self.slider.blockSignals(False)

    def set_range_label(self, start: float | None, end: float | None) -> None:
        self._range = (start, end)
        self._update_label(self.slider.value() / 100.0)

    def set_position(self, seconds: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(seconds * 100)))
        self.slider.blockSignals(False)
        self._update_label(seconds)

    def _on_value(self, value: int) -> None:
        seconds = value / 100.0
        self._update_label(seconds)
        self.position_chosen.emit(seconds)

    def _update_label(self, seconds: float) -> None:
        if self._duration <= 0:
            self.label.setText("—")
            return
        text = f"{format_timecode(seconds)} / {format_timecode(self._duration)}"
        start, end = self._range
        if start is not None and end is not None:
            text += f"  · выход {format_timecode(start)}–{format_timecode(end)}"
        self.label.setText(text)
