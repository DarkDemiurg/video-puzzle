from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QDragEnterEvent, QDropEvent, QMouseEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from video_puzzle.state import is_video_file

FILE_FILTER = "Видео (*.mp4 *.mov *.mkv *.webm *.avi *.m4v);;Все файлы (*)"
SLOT_MIME = "application/x-video-puzzle-slot"


def paths_from_urls(urls: list) -> list[Path]:
    return [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]


class SlotWidget(QFrame):
    files_dropped = Signal(int, list)
    file_picked = Signal(int, Path)
    cleared = Signal(int)
    trim_requested = Signal(int)
    swap_requested = Signal(int, int)

    def __init__(self, index: int, parent=None) -> None:
        super().__init__(parent)
        self.index = index
        self._pixmap: QPixmap | None = None
        self._wall_mode = False
        self._press_pos = None
        self._dragged = False
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(QSize(140, 80))
        self.setObjectName("slot")
        self.setProperty("filled", False)

        self.preview = QLabel("Перетащите видео\nили нажмите, чтобы выбрать")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setObjectName("slotPreview")
        self.preview.setWordWrap(True)

        self.badge = QLabel("")
        self.badge.setObjectName("slotBadge")
        self.badge.setVisible(False)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.filename = QLabel("")
        self.filename.setObjectName("slotFilename")
        self.filename.setVisible(False)
        self.filename.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.mark_btn = QPushButton("I/O")
        self.mark_btn.setObjectName("slotMark")
        self.mark_btn.setFixedHeight(28)
        self.mark_btn.setVisible(False)
        self.mark_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mark_btn.setToolTip("Разметить фрагмент")
        self.mark_btn.clicked.connect(lambda: self.trim_requested.emit(self.index))

        self.clear_btn = QPushButton("×")
        self.clear_btn.setObjectName("slotClear")
        self.clear_btn.setFixedSize(28, 28)
        self.clear_btn.setVisible(False)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip("Убрать файл")
        self.clear_btn.clicked.connect(lambda: self.cleared.emit(self.index))

        caption = QHBoxLayout()
        caption.setContentsMargins(0, 0, 0, 0)
        caption.addWidget(self.filename, 1)
        caption.addWidget(self.mark_btn)
        caption.addWidget(self.clear_btn, 0, Qt.AlignmentFlag.AlignRight)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self.preview, 1)
        layout.addLayout(caption)
        self.badge.setParent(self)

    def set_wall_mode(self, enabled: bool) -> None:
        self._wall_mode = enabled
        self._refresh_mark_btn()

    def minimumSizeHint(self) -> QSize:
        return QSize(180, 110)

    def set_empty(self) -> None:
        self._pixmap = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Перетащите видео\nили нажмите, чтобы выбрать")
        self.filename.setVisible(False)
        self.clear_btn.setVisible(False)
        self.set_fragment_label(None)
        self._set_filled(False)
        self._refresh_mark_btn()

    def set_loading(self, path: Path) -> None:
        self._pixmap = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Извлекаю кадр…")
        self.filename.setText(path.name)
        self.filename.setVisible(True)
        self.clear_btn.setVisible(True)
        self._set_filled(True)
        self._refresh_mark_btn()

    def set_thumbnail(self, path: Path, image: Path) -> None:
        self._pixmap = QPixmap(str(image))
        self.filename.setText(path.name)
        self.filename.setVisible(True)
        self.clear_btn.setVisible(True)
        self._set_filled(True)
        self._refresh_mark_btn()
        self._scale_preview()

    def set_error(self, path: Path, message: str) -> None:
        self._pixmap = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText(f"Нет превью\n{message}")
        self.filename.setText(path.name)
        self.filename.setVisible(True)
        self.clear_btn.setVisible(True)
        self._set_filled(True)
        self._refresh_mark_btn()

    def set_fragment_label(self, text: str | None) -> None:
        if text:
            self.badge.setText(text)
            self.badge.setVisible(True)
            self.badge.adjustSize()
            self.badge.raise_()
            self._place_badge()
        else:
            self.badge.hide()

    def _refresh_mark_btn(self) -> None:
        self.mark_btn.setVisible(self._wall_mode and self.clear_btn.isVisible())

    def _set_filled(self, filled: bool) -> None:
        self.setProperty("filled", filled)
        self.style().unpolish(self)
        self.style().polish(self)

    def _scale_preview(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        scaled = self._pixmap.scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setText("")
        self.preview.setPixmap(scaled)

    def _place_badge(self) -> None:
        if self.badge.isVisible():
            self.badge.move(14, 14)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._scale_preview()
        self._place_badge()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._wall_mode:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if child in {self.clear_btn, self.mark_btn, self.badge}:
                return
            self._press_pos = event.position()
            self._dragged = False
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_pos is None or not self.clear_btn.isVisible():
            return
        if (event.position() - self._press_pos).manhattanLength() < 8:
            return
        self._dragged = True
        mime = QMimeData()
        mime.setData(SLOT_MIME, QByteArray(str(self.index).encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        if self._pixmap is not None and not self._pixmap.isNull():
            drag.setPixmap(self._pixmap.scaled(96, 54, Qt.AspectRatioMode.KeepAspectRatio))
        drag.exec(Qt.DropAction.MoveAction)
        self._press_pos = None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            super().mouseReleaseEvent(event)
            return
        dragged = self._dragged
        self._press_pos = None
        self._dragged = False
        if dragged:
            return
        if self._wall_mode and self.clear_btn.isVisible():
            self.trim_requested.emit(self.index)
            return
        self._pick_file()

    def _pick_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Выберите видеофайл", "", FILE_FILTER)
        if not chosen:
            return
        path = Path(chosen)
        if is_video_file(path):
            self.file_picked.emit(self.index, path)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(SLOT_MIME) or event.mimeData().hasUrls():
            if event.mimeData().hasUrls():
                paths = paths_from_urls(event.mimeData().urls())
                if not any(is_video_file(path) for path in paths):
                    event.ignore()
                    return
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasFormat(SLOT_MIME):
            raw = bytes(event.mimeData().data(SLOT_MIME)).decode("utf-8")
            try:
                source = int(raw)
            except ValueError:
                event.ignore()
                return
            if source != self.index:
                self.swap_requested.emit(source, self.index)
            event.acceptProposedAction()
            return
        paths = [path for path in paths_from_urls(event.mimeData().urls()) if is_video_file(path)]
        if paths:
            self.files_dropped.emit(self.index, paths)
            event.acceptProposedAction()
        else:
            event.ignore()
