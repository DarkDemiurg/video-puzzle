from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from video_puzzle.progress import format_timecode
from video_puzzle.state import Slot
from video_puzzle.thumbnails import ThumbnailError, extract_thumbnail
from video_puzzle.ui.timeline import TimelineBar
from video_puzzle.wall import clamp_marks


class _FrameWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, video: Path, dest: Path, at: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._video = video
        self._dest = dest
        self._at = at

    def run(self) -> None:
        try:
            extract_thumbnail(self._video, self._dest, at_seconds=self._at)
            self.succeeded.emit(str(self._dest))
        except ThumbnailError as exc:
            self.failed.emit(str(exc))


class TrimEditor(QDialog):
    """Source-monitor style in/out editor (I/O, scrub, nudge)."""

    def __init__(self, slot: Slot, cache: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Разметка фрагмента")
        self.resize(820, 560)
        assert slot.path is not None
        self._path = slot.path
        self._duration = slot.duration or 0.0
        self._cache = cache
        self._gen = 0
        self._worker: _FrameWorker | None = None
        self._frame_pending = False
        self._closed = False
        default_out = self._duration if self._duration > 0 else 1.0
        self._mark_in = slot.mark_in if slot.mark_in is not None else 0.0
        self._mark_out = slot.mark_out if slot.mark_out is not None else default_out
        self._mark_in, self._mark_out = clamp_marks(
            self._duration or default_out, self._mark_in, self._mark_out
        )
        self._playhead = self._mark_in

        self.preview = QLabel("Кадр")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(QSize(480, 270))
        self.preview.setObjectName("trimPreview")
        self._pixmap: QPixmap | None = None

        self.timeline = TimelineBar()
        self.timeline.position_chosen.connect(self._on_scrub)
        self.timeline.set_duration(self._duration if self._duration > 0 else None)

        self.status = QLabel()
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)

        keys = QLabel("I — вход  ·  O — выход  ·  ←/→ кадр  ·  Shift+←/→ 1 с  ·  Home/End")
        keys.setObjectName("hint")

        row = QHBoxLayout()
        self.in_btn = QPushButton("I  Вход")
        self.out_btn = QPushButton("O  Выход")
        self.go_in_btn = QPushButton("К входу")
        self.go_out_btn = QPushButton("К выходу")
        for button, slot_fn in (
            (self.in_btn, self._mark_in_here),
            (self.out_btn, self._mark_out_here),
            (self.go_in_btn, self._go_in),
            (self.go_out_btn, self._go_out),
        ):
            button.setObjectName("secondary")
            button.clicked.connect(slot_fn)
            row.addWidget(button)

        nudge = QHBoxLayout()
        for label, delta in (
            ("-1 с", -1.0),
            ("-1 кадр", -1 / 30),
            ("+1 кадр", 1 / 30),
            ("+1 с", 1.0),
        ):
            button = QPushButton(label)
            button.setObjectName("secondary")
            button.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            nudge.addWidget(button)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        apply = QPushButton("Применить фрагмент")
        apply.setObjectName("primary")
        apply.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.timeline)
        layout.addWidget(self.status)
        layout.addLayout(row)
        layout.addLayout(nudge)
        layout.addWidget(keys)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._pull_frame)
        self._refresh_status()
        self.timeline.set_position(self._playhead)
        self.timeline.set_range_label(self._mark_in, self._mark_out)
        self._pull_frame()

    def marks(self) -> tuple[float, float]:
        return clamp_marks(self._duration or self._mark_out, self._mark_in, self._mark_out)

    def _on_scrub(self, seconds: float) -> None:
        self._playhead = self._clamp_playhead(seconds)
        self._refresh_status()
        self._timer.start()

    def _clamp_playhead(self, seconds: float) -> float:
        if self._duration <= 0:
            return max(0.0, seconds)
        return min(max(0.0, seconds), max(0.0, self._duration - 0.04))

    def _nudge(self, delta: float) -> None:
        self._playhead = self._clamp_playhead(self._playhead + delta)
        self.timeline.set_position(self._playhead)
        self._refresh_status()
        self._timer.start()

    def _mark_in_here(self) -> None:
        self._mark_in = self._playhead
        if self._mark_out <= self._mark_in:
            self._mark_out = min(self._duration or self._mark_in + 0.04, self._mark_in + 0.04)
        self._refresh_status()

    def _mark_out_here(self) -> None:
        self._mark_out = max(self._playhead, self._mark_in + 0.04)
        self._refresh_status()

    def _go_in(self) -> None:
        self._playhead = self._mark_in
        self.timeline.set_position(self._playhead)
        self._timer.start()

    def _go_out(self) -> None:
        self._playhead = self._mark_out
        self.timeline.set_position(self._playhead)
        self._timer.start()

    def _refresh_status(self) -> None:
        length = max(0.0, self._mark_out - self._mark_in)
        self.status.setText(
            f"{self._path.name}\n"
            f"Курсор {format_timecode(self._playhead)}  ·  "
            f"In {format_timecode(self._mark_in)}  ·  "
            f"Out {format_timecode(self._mark_out)}  ·  "
            f"Длина {format_timecode(length)}"
        )
        self.timeline.set_range_label(self._mark_in, self._mark_out)

    def _pull_frame(self) -> None:
        if self._closed:
            return
        if self._worker is not None and self._worker.isRunning():
            self._frame_pending = True
            return
        self._frame_pending = False
        self._gen += 1
        dest = self._cache / f"trim-{self._gen}.jpg"
        worker = _FrameWorker(self._path, dest, self._playhead, self)
        worker.succeeded.connect(self._on_frame)
        worker.failed.connect(self._on_frame_fail)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_frame(self, image: str) -> None:
        if self._closed:
            return
        self._pixmap = QPixmap(image)
        self._scale()

    def _on_frame_fail(self, message: str) -> None:
        if not self._closed:
            self.preview.setText(message)

    def _on_worker_finished(self) -> None:
        if self.sender() is self._worker:
            self._worker = None
        if self._closed:
            return
        if self._frame_pending:
            self._pull_frame()

    def _scale(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        scaled = self._pixmap.scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(scaled)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_worker()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self._stop_worker()
        super().done(result)

    def _stop_worker(self) -> None:
        self._closed = True
        self._timer.stop()
        self._frame_pending = False
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.wait(15_000)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._scale()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key.Key_I:
            self._mark_in_here()
            return
        if key == Qt.Key.Key_O:
            self._mark_out_here()
            return
        if key == Qt.Key.Key_Left:
            self._nudge(-1.0 if mods & Qt.KeyboardModifier.ShiftModifier else -1 / 30)
            return
        if key == Qt.Key.Key_Right:
            self._nudge(1.0 if mods & Qt.KeyboardModifier.ShiftModifier else 1 / 30)
            return
        if key == Qt.Key.Key_Home:
            self._playhead = 0.0
            self.timeline.set_position(self._playhead)
            self._timer.start()
            return
        if key == Qt.Key.Key_End:
            self._playhead = self._clamp_playhead(self._duration)
            self.timeline.set_position(self._playhead)
            self._timer.start()
            return
        super().keyPressEvent(event)
