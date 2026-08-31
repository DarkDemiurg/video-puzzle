from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from video_puzzle.encode import (
    DEFAULT_ENCODER,
    DEFAULT_QUALITY,
    EncodeQuality,
    EncoderKind,
    estimate_output_bytes,
    format_size,
)
from video_puzzle.encoders import detect_encoders
from video_puzzle.ffmpeg_script import (
    IncompleteStateError,
    build_ffmpeg_args,
    build_still_args,
    mosaic_output_size,
    render_shell_script,
)
from video_puzzle.layout import MAX_SLOTS, RESOLUTIONS, AppMode, Layout
from video_puzzle.probe import ProbeError, probe_video
from video_puzzle.progress import (
    format_timecode,
    parse_progress_seconds,
    progress_fraction,
    render_outcome,
    should_delete_partial_output,
)
from video_puzzle.project import ProjectError, load_project, save_project
from video_puzzle.state import AppState, Slot
from video_puzzle.sync import SyncError, SyncResult, analyze_slots, apply_sync
from video_puzzle.thumbnails import ThumbnailError, extract_thumbnail
from video_puzzle.ui.preview_canvas import PreviewCanvas
from video_puzzle.ui.timeline import TimelineBar
from video_puzzle.ui.trim_editor import TrimEditor
from video_puzzle.wall import AutoFragmentError, apply_shortest_from_start, inspect_wall


class ThumbnailWorker(QThread):
    succeeded = Signal(int, int, str)
    failed = Signal(int, int, str)

    def __init__(
        self, index: int, generation: int, video: Path, dest: Path, at_seconds: float
    ) -> None:
        super().__init__()
        self._index = index
        self._generation = generation
        self._video = video
        self._dest = dest
        self._at = at_seconds

    def run(self) -> None:
        try:
            extract_thumbnail(self._video, self._dest, at_seconds=self._at)
            self.succeeded.emit(self._index, self._generation, str(self._dest))
        except ThumbnailError as exc:
            self.failed.emit(self._index, self._generation, str(exc))


class ProbeWorker(QThread):
    succeeded = Signal(int, float, bool, float)
    failed = Signal(int, str)

    def __init__(self, index: int, video: Path) -> None:
        super().__init__()
        self._index = index
        self._video = video

    def run(self) -> None:
        try:
            result = probe_video(self._video)
            self.succeeded.emit(self._index, result.duration, result.has_audio, result.fps or 0.0)
        except ProbeError as exc:
            self.failed.emit(self._index, str(exc))


class StillWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, args: list[str], dest: Path) -> None:
        super().__init__()
        self._args = args
        self._dest = dest

    def run(self) -> None:
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(self._args, capture_output=True, text=True, check=False)
            if result.returncode != 0 or not self._dest.is_file():
                detail = (result.stderr or result.stdout or "").strip()
                self.failed.emit(detail or "Не удалось собрать кадр склейки")
                return
            self.succeeded.emit(str(self._dest))
        except OSError as exc:
            self.failed.emit(str(exc))


class SyncWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self._slots = [
            Slot(path=slot.path, duration=slot.duration, has_audio=slot.has_audio)
            for slot in state.active_slots()
        ]

    def run(self) -> None:
        try:
            result = analyze_slots(self._slots)
            self.succeeded.emit(result)
        except (SyncError, ValueError) as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Video Puzzle")
        self.state = AppState()
        self._settings = QSettings()
        self._encoders = detect_encoders()
        self._thumb_gen = [0] * MAX_SLOTS
        self._workers: list[QThread] = []
        self._proc: QProcess | None = None
        self._still_worker: StillWorker | None = None
        self._render_total = 0.0
        self._render_cancelled = False
        self._render_output: Path | None = None
        self._render_log = ""
        self._progress_buf = ""
        self._closing = False
        self._cache = (
            Path(
                QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
                or "/tmp"
            )
            / "video-puzzle"
        )
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.setInterval(120)
        self._scrub_timer.timeout.connect(self._refresh_all_thumbnails)

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)

        right_pane = QWidget()
        right = QVBoxLayout(right_pane)
        right.setContentsMargins(0, 0, 0, 0)
        self.canvas = PreviewCanvas()
        right.addWidget(self.canvas, 1)
        self.puzzle_extras = QWidget()
        puzzle_layout = QVBoxLayout(self.puzzle_extras)
        puzzle_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline_hint = QLabel(
            "Клик по шкале ставит один момент во все превью. "
            "Фрагмент выхода задаётся на этой же шкале, например с 20 по 60 секунды."
        )
        self.timeline_hint.setObjectName("hint")
        self.timeline_hint.setWordWrap(True)
        puzzle_layout.addWidget(self.timeline_hint)
        self.timeline = TimelineBar()
        self.timeline.position_chosen.connect(self._on_playhead)
        puzzle_layout.addWidget(self.timeline)
        puzzle_layout.addWidget(self._build_range_row())
        align_row = QHBoxLayout()
        self.sync_btn = QPushButton("Синхронизировать по звуку")
        self.sync_btn.setObjectName("secondary")
        self.sync_btn.clicked.connect(self._sync_audio)
        self.align_note = QLabel("")
        self.align_note.setObjectName("alignNote")
        self.align_note.setWordWrap(True)
        align_row.addWidget(self.sync_btn)
        align_row.addWidget(self.align_note, 1)
        puzzle_layout.addLayout(align_row)
        right.addWidget(self.puzzle_extras)
        self.wall_hint = QLabel(
            "Клик по слоту открывает разметку фрагмента (I/O, как в монтажке). "
            "«Фрагменты по короткому» ставит всем вход в начале ролика "
            "и длину самого короткого файла. "
            "Превью — первый кадр фрагмента, длина показана на слоте."
        )
        self.wall_hint.setObjectName("hint")
        self.wall_hint.setWordWrap(True)
        self.wall_hint.setVisible(False)
        right.addWidget(self.wall_hint)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.cancel_progress_btn = QPushButton("Остановить")
        self.cancel_progress_btn.setObjectName("danger")
        self.cancel_progress_btn.setVisible(False)
        self.cancel_progress_btn.clicked.connect(self._cancel_render)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel_progress_btn)
        right.addLayout(progress_row)
        self.script_view = QPlainTextEdit()
        self.script_view.setObjectName("scriptPreview")
        self.script_view.setReadOnly(True)
        self.script_view.setPlaceholderText(
            "Здесь появится команда ffmpeg, когда все слоты заполнены."
        )
        self.script_view.setFixedHeight(120)
        right.addWidget(self.script_view)
        extra_row = QHBoxLayout()
        self.preview_mosaic_btn = QPushButton("Кадр склейки")
        self.preview_mosaic_btn.setObjectName("secondary")
        self.preview_mosaic_btn.clicked.connect(self._preview_mosaic)
        extra_row.addWidget(self.preview_mosaic_btn)
        extra_row.addStretch(1)
        right.addLayout(extra_row)
        self.splitter.addWidget(self._build_sidebar())
        self.splitter.addWidget(right_pane)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter)

        for slot in self.canvas.slots:
            self._bind_slot(slot)

        self._restore_settings()
        self._sync_ui()
        if not self._geometry_restored:
            self.resize(1180, 780)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Video Puzzle")
        title.setObjectName("title")
        hint = QLabel("Пазл — 2–4 ролика. Видеостена — сетка со своим фрагментом у каждого слота.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)
        project_row = QHBoxLayout()
        self.open_project_btn = QPushButton("Открыть проект…")
        self.open_project_btn.setObjectName("secondary")
        self.save_project_btn = QPushButton("Сохранить проект…")
        self.save_project_btn.setObjectName("secondary")
        self.open_project_btn.clicked.connect(self._open_project)
        self.save_project_btn.clicked.connect(self._save_project)
        project_row.addWidget(self.open_project_btn)
        project_row.addWidget(self.save_project_btn)
        layout.addLayout(project_row)

        mode = QGroupBox("Режим")
        mode_layout = QVBoxLayout(mode)
        self.radio_puzzle = QRadioButton("Пазл")
        self.radio_wall = QRadioButton("Видеостена")
        self.radio_puzzle.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.radio_puzzle)
        self.mode_group.addButton(self.radio_wall)
        mode_layout.addWidget(self.radio_puzzle)
        mode_layout.addWidget(self.radio_wall)
        self.radio_puzzle.toggled.connect(self._on_mode_changed)
        layout.addWidget(mode)

        self.scheme_box = QGroupBox("Схема склейки")
        scheme_layout = QVBoxLayout(self.scheme_box)
        self.radio_2 = QRadioButton("2 файла")
        self.radio_3 = QRadioButton("3 файла — пирамида")
        self.radio_4 = QRadioButton("4 файла — квадрат")
        self.radio_4.setChecked(True)
        self.count_group = QButtonGroup(self)
        for button in (self.radio_2, self.radio_3, self.radio_4):
            self.count_group.addButton(button)

        self.two_wrap = QWidget()
        two_layout = QVBoxLayout(self.two_wrap)
        two_layout.setContentsMargins(18, 0, 0, 0)
        self.radio_h = QRadioButton("Горизонтально")
        self.radio_v = QRadioButton("Вертикально")
        self.radio_h.setChecked(True)
        self.orient_group = QButtonGroup(self)
        self.orient_group.addButton(self.radio_h)
        self.orient_group.addButton(self.radio_v)
        two_layout.addWidget(self.radio_h)
        two_layout.addWidget(self.radio_v)

        self.pyramid_wrap = QWidget()
        pyramid_layout = QVBoxLayout(self.pyramid_wrap)
        pyramid_layout.setContentsMargins(18, 0, 0, 0)
        self.radio_pyramid_wide = QRadioButton("Широкая пирамида")
        self.radio_pyramid_narrow = QRadioButton("Узкая пирамида")
        self.radio_pyramid_wide.setChecked(True)
        self.pyramid_group = QButtonGroup(self)
        self.pyramid_group.addButton(self.radio_pyramid_wide)
        self.pyramid_group.addButton(self.radio_pyramid_narrow)
        pyramid_layout.addWidget(self.radio_pyramid_wide)
        pyramid_layout.addWidget(self.radio_pyramid_narrow)

        scheme_layout.addWidget(self.radio_2)
        scheme_layout.addWidget(self.two_wrap)
        scheme_layout.addWidget(self.radio_3)
        scheme_layout.addWidget(self.pyramid_wrap)
        scheme_layout.addWidget(self.radio_4)
        self._sync_scheme_options()
        layout.addWidget(self.scheme_box)

        self.wall_box = QGroupBox("Сетка видеостены")
        wall_layout = QVBoxLayout(self.wall_box)
        grid_row = QHBoxLayout()
        self.rows_spin = QSpinBox()
        self.cols_spin = QSpinBox()
        for spin, label in ((self.rows_spin, "Строки"), (self.cols_spin, "Столбцы")):
            spin.setRange(1, 8)
            spin.setValue(2)
            wrap = QVBoxLayout()
            caption = QLabel(label)
            caption.setObjectName("hint")
            wrap.addWidget(caption)
            wrap.addWidget(spin)
            grid_row.addLayout(wrap)
        self.rows_spin.valueChanged.connect(self._on_wall_grid)
        self.cols_spin.valueChanged.connect(self._on_wall_grid)
        wall_layout.addLayout(grid_row)
        self.auto_frag_btn = QPushButton("Фрагменты по короткому")
        self.auto_frag_btn.setObjectName("secondary")
        self.auto_frag_btn.setToolTip(
            "Всем роликам: вход в начале файла, длина как у самого короткого."
        )
        self.auto_frag_btn.clicked.connect(self._auto_wall_fragments)
        wall_layout.addWidget(self.auto_frag_btn)
        self.wall_box.setVisible(False)
        layout.addWidget(self.wall_box)

        out_row = QHBoxLayout()
        res = QGroupBox("Разрешение")
        res_layout = QVBoxLayout(res)
        self.res_group = QButtonGroup(self)
        self.res_buttons: dict[int, QRadioButton] = {}
        for height, (width, _) in RESOLUTIONS.items():
            button = QRadioButton(f"{height}p")
            button.setToolTip(f"{width}×{height}")
            self.res_group.addButton(button)
            self.res_buttons[height] = button
            res_layout.addWidget(button)
        self.res_buttons[1080].setChecked(True)
        res_layout.addStretch(1)
        out_row.addWidget(res)

        quality = QGroupBox("Качество")
        quality_layout = QVBoxLayout(quality)
        self.quality_group = QButtonGroup(self)
        self.quality_buttons: dict[EncodeQuality, QRadioButton] = {}
        quality_labels = {
            EncodeQuality.DRAFT: "Быстрое",
            EncodeQuality.STANDARD: "Обычное",
            EncodeQuality.HIGH: "Высокое",
            EncodeQuality.ORIGINAL: "Как оригинал",
        }
        quality_tips = {
            EncodeQuality.DRAFT: "Быстрее и легче, заметнее сжатие.",
            EncodeQuality.STANDARD: "Баланс размера и качества (как раньше).",
            EncodeQuality.HIGH: "Меньше артефактов, сборка дольше.",
            EncodeQuality.ORIGINAL: "Почти без потерь после склейки, файл заметно больше.",
        }
        for kind, label in quality_labels.items():
            button = QRadioButton(label)
            button.setToolTip(quality_tips[kind])
            self.quality_group.addButton(button)
            self.quality_buttons[kind] = button
            quality_layout.addWidget(button)
        self.quality_buttons[DEFAULT_QUALITY].setChecked(True)
        out_row.addWidget(quality)
        layout.addLayout(out_row)

        encoder = QGroupBox("Кодек")
        encoder_layout = QVBoxLayout(encoder)
        self.encoder_group = QButtonGroup(self)
        self.encoder_buttons: dict[EncoderKind, QRadioButton] = {}
        encoder_labels = {
            EncoderKind.AUTO: "Авто (NVENC / QSV / CPU)",
            EncoderKind.CPU: "CPU  (libx264)",
            EncoderKind.NVENC: "NVIDIA  (NVENC)",
            EncoderKind.QSV: "Intel  (QSV)",
        }
        for kind, label in encoder_labels.items():
            button = QRadioButton(label)
            self.encoder_group.addButton(button)
            self.encoder_buttons[kind] = button
            encoder_layout.addWidget(button)
        self.encoder_buttons[DEFAULT_ENCODER].setChecked(True)
        if "h264_nvenc" not in self._encoders:
            self.encoder_buttons[EncoderKind.NVENC].setEnabled(False)
            self.encoder_buttons[EncoderKind.NVENC].setToolTip(
                "NVENC недоступен: ffmpeg не смог открыть кодек (часто нет CUDA / libcuda.so.1)."
            )
        if "h264_qsv" not in self._encoders:
            self.encoder_buttons[EncoderKind.QSV].setEnabled(False)
            self.encoder_buttons[EncoderKind.QSV].setToolTip(
                "Intel QSV недоступен на этой машине."
            )
        layout.addWidget(encoder)

        gap_box = QGroupBox("Зазор между ячейками")
        gap_layout = QHBoxLayout(gap_box)
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 40)
        self.gap_spin.setSingleStep(2)
        self.gap_spin.setSuffix(" px")
        self.gap_spin.valueChanged.connect(self._on_gap_changed)
        gap_layout.addWidget(self.gap_spin)
        layout.addWidget(gap_box)

        self.audio_check = QCheckBox("Включить звук")
        self.audio_check.setChecked(True)
        self.audio_check.toggled.connect(self._on_audio_toggled)
        layout.addWidget(self.audio_check)
        self.audio_combo = QComboBox()
        self.audio_combo.currentIndexChanged.connect(self._on_audio_slot_changed)
        layout.addWidget(self.audio_combo)
        self.normalize_check = QCheckBox("Нормализовать громкость")
        self.normalize_check.toggled.connect(self._on_normalize_toggled)
        layout.addWidget(self.normalize_check)

        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.save_btn = QPushButton("Сохранить скрипт…")
        self.save_btn.setObjectName("secondary")
        self.save_btn.clicked.connect(self._save_script)
        self.render_btn = QPushButton("Собрать видео…")
        self.render_btn.setObjectName("primary")
        self.render_btn.clicked.connect(self._render)
        self.cancel_btn = QPushButton("Остановить сборку")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_render)
        layout.addWidget(self.save_btn)
        layout.addWidget(self.render_btn)
        layout.addWidget(self.cancel_btn)
        layout.addStretch(1)

        self.radio_2.toggled.connect(self._on_scheme_changed)
        self.radio_3.toggled.connect(self._on_scheme_changed)
        self.radio_4.toggled.connect(self._on_scheme_changed)
        self.radio_h.toggled.connect(self._on_scheme_changed)
        self.radio_v.toggled.connect(self._on_scheme_changed)
        self.radio_pyramid_wide.toggled.connect(self._on_scheme_changed)
        self.radio_pyramid_narrow.toggled.connect(self._on_scheme_changed)
        for button in self.res_buttons.values():
            button.toggled.connect(self._on_resolution_changed)
        for button in self.quality_buttons.values():
            button.toggled.connect(self._on_quality_changed)
        for button in self.encoder_buttons.values():
            button.toggled.connect(self._on_encoder_changed)
        return self._wrap_sidebar(sidebar)

    def _wrap_sidebar(self, sidebar: QWidget) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("sidebarScroll")
        scroll.setWidget(sidebar)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(240)
        scroll.setMaximumWidth(640)
        scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        return scroll

    def _build_range_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 0, 12, 4)
        self.range_check = QCheckBox("Фрагмент выхода")
        self.range_check.toggled.connect(self._on_range_toggled)
        self.range_start = QDoubleSpinBox()
        self.range_end = QDoubleSpinBox()
        for spin in (self.range_start, self.range_end):
            spin.setDecimals(2)
            spin.setRange(0.0, 86400.0)
            spin.setSuffix(" с")
            spin.setSingleStep(1.0)
            spin.setEnabled(False)
            spin.valueChanged.connect(self._on_range_spins)
        self.mark_start_btn = QPushButton("Начало с курсора")
        self.mark_end_btn = QPushButton("Конец с курсора")
        for button in (self.mark_start_btn, self.mark_end_btn):
            button.setObjectName("secondary")
            button.setEnabled(False)
        self.mark_start_btn.clicked.connect(self._mark_start)
        self.mark_end_btn.clicked.connect(self._mark_end)
        layout.addWidget(self.range_check)
        layout.addWidget(QLabel("с"))
        layout.addWidget(self.range_start)
        layout.addWidget(self.mark_start_btn)
        layout.addWidget(QLabel("по"))
        layout.addWidget(self.range_end)
        layout.addWidget(self.mark_end_btn)
        layout.addStretch(1)
        return row

    def _selected_layout(self) -> Layout:
        if self.radio_2.isChecked():
            return Layout.TWO_HORIZONTAL if self.radio_h.isChecked() else Layout.TWO_VERTICAL
        if self.radio_3.isChecked():
            if self.radio_pyramid_narrow.isChecked():
                return Layout.THREE_PYRAMID_NARROW
            return Layout.THREE_PYRAMID
        return Layout.FOUR_SQUARE

    def _bind_slot(self, widget) -> None:
        widget.file_picked.connect(self._on_file_picked)
        widget.files_dropped.connect(self._on_files_dropped)
        widget.cleared.connect(self._on_slot_cleared)
        widget.trim_requested.connect(self._open_trim_editor)
        widget.swap_requested.connect(self._on_slots_swapped)

    def _apply_canvas_layout(self) -> None:
        gap = max(8, self.state.cell_gap)
        if self.state.is_wall:
            created = self.canvas.ensure_slots(self.state.active_count)
            for widget in created:
                self._bind_slot(widget)
            self.canvas.apply_grid(self.state.wall_rows, self.state.wall_cols, gap=gap)
        else:
            self.canvas.apply_layout(self.state.layout, gap=gap)
        self.canvas.set_wall_mode(self.state.is_wall)

    def _on_mode_changed(self) -> None:
        wall = self.radio_wall.isChecked()
        self.state.set_mode(AppMode.WALL if wall else AppMode.PUZZLE)
        self.scheme_box.setVisible(not wall)
        self.wall_box.setVisible(wall)
        self.puzzle_extras.setVisible(not wall)
        self.wall_hint.setVisible(wall)
        self._apply_canvas_layout()
        self._sync_ui()
        self._refresh_all_thumbnails()

    def _on_wall_grid(self) -> None:
        if not self.state.is_wall:
            return
        self.state.set_wall_grid(self.rows_spin.value(), self.cols_spin.value())
        self._apply_canvas_layout()
        self._sync_ui()

    def _open_trim_editor(self, index: int) -> None:
        slot = self.state.slots[index]
        if slot.path is None:
            return
        editor = TrimEditor(slot, self._cache, self)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        mark_in, mark_out = editor.marks()
        slot.set_marks(mark_in, mark_out)
        slot.rotation = editor.rotation()
        slot.crop = editor.crop()
        self._refresh_thumbnail(index, loading=False)
        self._sync_ui()

    def _on_slots_swapped(self, source: int, dest: int) -> None:
        self.state.swap_slots(source, dest)
        self._refresh_all_thumbnails()
        self._sync_ui()

    def _auto_wall_fragments(self) -> None:
        try:
            apply_shortest_from_start(self.state.slots, self.state.active_count)
        except AutoFragmentError as exc:
            QMessageBox.warning(self, "Видеостена", str(exc))
            return
        self._refresh_all_thumbnails()
        self._sync_ui()

    def _update_slot_badges(self) -> None:
        count = self.state.active_count
        for index, widget in enumerate(self.canvas.slots):
            if index >= count or not self.state.is_wall:
                widget.set_fragment_label(None)
                continue
            slot = self.state.slots[index]
            if slot.path is None:
                widget.set_fragment_label(None)
            elif slot.has_fragment and slot.fragment_duration is not None:
                widget.set_fragment_label(format_timecode(slot.fragment_duration))
            else:
                widget.set_fragment_label("нет фрагмента")

    def _confirm_export(self) -> bool:
        if not self.state.is_wall:
            return True
        report = inspect_wall(self.state.slots, self.state.active_count)
        if report.missing_files:
            QMessageBox.warning(self, "Видеостена", "Заполните все ячейки сетки.")
            return False
        if report.missing_fragments:
            labels = ", ".join(str(i + 1) for i in report.missing_fragments)
            choice = QMessageBox.question(
                self,
                "Нет фрагмента",
                f"У слотов {labels} не выбран фрагмент (I/O).\nОткрыть разметку первого из них?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self._open_trim_editor(report.missing_fragments[0])
            return False
        if report.length_warning:
            answer = QMessageBox.question(
                self,
                "Разная длина фрагментов",
                (
                    f"Фрагменты от {report.min_duration:.1f} до {report.max_duration:.1f} с "
                    f"(разница {report.spread:.1f} с).\n"
                    f"Стена будет собрана по самому короткому ({report.min_duration:.1f} с).\n"
                    "Продолжить?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return True

    def _sync_scheme_options(self) -> None:
        two = self.radio_2.isChecked()
        three = self.radio_3.isChecked()
        self.two_wrap.setVisible(two)
        self.pyramid_wrap.setVisible(three)
        self.radio_h.setEnabled(two)
        self.radio_v.setEnabled(two)
        self.radio_pyramid_wide.setEnabled(three)
        self.radio_pyramid_narrow.setEnabled(three)

    def _on_scheme_changed(self) -> None:
        self._sync_scheme_options()
        layout = self._selected_layout()
        if layout is self.state.layout:
            return
        self.state.set_layout(layout)
        self._apply_canvas_layout()
        self._sync_ui()
        self._refresh_all_thumbnails()

    def _on_resolution_changed(self) -> None:
        for height, button in self.res_buttons.items():
            if button.isChecked():
                self.state.set_resolution(height)
                self._sync_ui()
                return

    def _on_quality_changed(self) -> None:
        for quality, button in self.quality_buttons.items():
            if button.isChecked():
                self.state.set_quality(quality)
                self._settings.setValue("quality", str(quality))
                self._sync_ui()
                return

    def _on_encoder_changed(self) -> None:
        for kind, button in self.encoder_buttons.items():
            if button.isChecked():
                self.state.set_encoder(kind)
                self._settings.setValue("encoder", str(kind))
                self._sync_ui()
                return

    def _on_gap_changed(self, value: int) -> None:
        self.state.set_cell_gap(value)
        self._settings.setValue("cellGap", self.state.cell_gap)
        self._apply_canvas_layout()
        self._sync_ui()

    def _on_audio_toggled(self, checked: bool) -> None:
        self.state.include_audio = checked
        self.audio_combo.setEnabled(checked)
        self.normalize_check.setEnabled(checked)
        self._sync_ui()

    def _on_audio_slot_changed(self, index: int) -> None:
        data = self.audio_combo.itemData(index)
        self.state.audio_slot = int(data) if data is not None else None
        self._sync_ui()

    def _on_normalize_toggled(self, checked: bool) -> None:
        self.state.normalize_audio = checked
        self._sync_ui()

    def _on_range_toggled(self, checked: bool) -> None:
        self.state.set_range_enabled(checked)
        if checked and self.state.range_end is None:
            overlap = self.state.overlap_duration()
            if overlap is not None:
                self.state.set_output_range(self.state.range_start, overlap)
        self._sync_ui()

    def _on_range_spins(self) -> None:
        if not self.range_check.isChecked():
            return
        start = self.range_start.value()
        end = self.range_end.value()
        if end <= start:
            end = start + 0.04
        self.state.set_output_range(start, end)
        self._sync_ui()

    def _mark_start(self) -> None:
        if not self.state.range_enabled:
            self.state.set_range_enabled(True)
        end = self.state.resolved_range_end()
        start = self.state.playhead
        if end is not None and end <= start:
            end = start + 0.04
        self.state.set_output_range(start, end)
        self._sync_ui()

    def _mark_end(self) -> None:
        if not self.state.range_enabled:
            self.state.set_range_enabled(True)
        start = self.state.resolved_range_start()
        end = self.state.playhead
        if end <= start:
            start = max(0.0, end - 0.04)
        self.state.set_output_range(start, end)
        self._sync_ui()

    def _sync_range_widgets(self) -> None:
        enabled = self.state.range_enabled
        overlap = self.state.overlap_duration()
        self.range_check.blockSignals(True)
        self.range_check.setChecked(enabled)
        self.range_check.blockSignals(False)
        maximum = overlap if overlap is not None else 86400.0
        self.range_start.setMaximum(maximum)
        self.range_end.setMaximum(maximum)
        self.range_start.blockSignals(True)
        self.range_end.blockSignals(True)
        self.range_start.setValue(self.state.resolved_range_start() if enabled else 0.0)
        end = self.state.resolved_range_end()
        self.range_end.setValue(end if enabled and end is not None else (overlap or 0.0))
        self.range_start.blockSignals(False)
        self.range_end.blockSignals(False)
        probed = overlap is not None
        self.range_start.setEnabled(enabled)
        self.range_end.setEnabled(enabled)
        self.mark_start_btn.setEnabled(probed)
        self.mark_end_btn.setEnabled(probed)
        if enabled and end is not None:
            self.timeline.set_range_label(self.state.resolved_range_start(), end)
        else:
            self.timeline.set_range_label(None, None)

    def _restore_settings(self) -> None:
        self._geometry_restored = False
        geometry = self._settings.value("geometry")
        if geometry is not None:
            self._geometry_restored = bool(self.restoreGeometry(geometry))
        quality_raw = self._settings.value("quality")
        if quality_raw:
            try:
                quality = EncodeQuality(str(quality_raw))
            except ValueError:
                quality = None
            if quality is not None and quality in self.quality_buttons:
                self.quality_buttons[quality].setChecked(True)
                self.state.set_quality(quality)
        encoder_raw = self._settings.value("encoder")
        if encoder_raw:
            try:
                encoder = EncoderKind(str(encoder_raw))
            except ValueError:
                encoder = None
            if encoder is not None and encoder in self.encoder_buttons:
                button = self.encoder_buttons[encoder]
                if button.isEnabled():
                    button.setChecked(True)
                    self.state.set_encoder(encoder)
        gap_raw = self._settings.value("cellGap")
        if gap_raw is not None:
            try:
                gap = int(gap_raw)
            except (TypeError, ValueError):
                gap = None
            if gap is not None:
                self.gap_spin.blockSignals(True)
                self.gap_spin.setValue(gap)
                self.gap_spin.blockSignals(False)
                self.state.set_cell_gap(gap)
        splitter_state = self._settings.value("splitter")
        if splitter_state is None or not self.splitter.restoreState(splitter_state):
            self.splitter.setSizes([300, 880])
        self._apply_canvas_layout()

    def _scheme_widgets(self) -> list[QWidget]:
        return [
            self.radio_puzzle,
            self.radio_wall,
            self.radio_2,
            self.radio_3,
            self.radio_4,
            self.radio_h,
            self.radio_v,
            self.radio_pyramid_wide,
            self.radio_pyramid_narrow,
            self.rows_spin,
            self.cols_spin,
            self.gap_spin,
            self.audio_check,
            self.normalize_check,
            *self.res_buttons.values(),
            *self.quality_buttons.values(),
            *self.encoder_buttons.values(),
        ]

    def _apply_state_to_widgets(self) -> None:
        for index in range(len(self._thumb_gen)):
            self._thumb_gen[index] += 1
        widgets = self._scheme_widgets()
        for widget in widgets:
            widget.blockSignals(True)
        wall = self.state.is_wall
        self.radio_wall.setChecked(wall)
        self.radio_puzzle.setChecked(not wall)
        layout = self.state.layout
        if layout in {Layout.TWO_HORIZONTAL, Layout.TWO_VERTICAL}:
            self.radio_2.setChecked(True)
            self.radio_h.setChecked(layout is Layout.TWO_HORIZONTAL)
            self.radio_v.setChecked(layout is Layout.TWO_VERTICAL)
        elif layout is Layout.THREE_PYRAMID_NARROW:
            self.radio_3.setChecked(True)
            self.radio_pyramid_narrow.setChecked(True)
        elif layout is Layout.THREE_PYRAMID:
            self.radio_3.setChecked(True)
            self.radio_pyramid_wide.setChecked(True)
        else:
            self.radio_4.setChecked(True)
        self._sync_scheme_options()
        self.rows_spin.setValue(self.state.wall_rows)
        self.cols_spin.setValue(self.state.wall_cols)
        if self.state.resolution in self.res_buttons:
            self.res_buttons[self.state.resolution].setChecked(True)
        if self.state.quality in self.quality_buttons:
            self.quality_buttons[self.state.quality].setChecked(True)
        encoder_button = self.encoder_buttons.get(self.state.encoder)
        if encoder_button is not None and encoder_button.isEnabled():
            encoder_button.setChecked(True)
        else:
            self.encoder_buttons[DEFAULT_ENCODER].setChecked(True)
            self.state.set_encoder(DEFAULT_ENCODER)
        self.gap_spin.setValue(self.state.cell_gap)
        self.audio_check.setChecked(self.state.include_audio)
        self.normalize_check.setChecked(self.state.normalize_audio)
        self.scheme_box.setVisible(not wall)
        self.wall_box.setVisible(wall)
        self.puzzle_extras.setVisible(not wall)
        self.wall_hint.setVisible(wall)
        for widget in widgets:
            widget.blockSignals(False)
        self._apply_canvas_layout()
        count = self.state.active_count
        for index, widget in enumerate(self.canvas.slots):
            slot = self.state.slots[index] if index < len(self.state.slots) else None
            if index >= count or slot is None or slot.path is None:
                widget.set_empty()
                continue
            self._probe_slot(index, slot.path)
            self._request_thumbnail(index, slot.path, loading=True)
        self._sync_ui()

    def _save_project(self) -> None:
        last = self._settings.value("lastProjectDir") or str(Path.home())
        suggested = str(Path(str(last)) / "puzzle.vproj")
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить проект",
            suggested,
            "Проект Video Puzzle (*.vproj *.json);;Все файлы (*)",
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() not in {".vproj", ".json"}:
            path = path.with_suffix(".vproj")
        try:
            save_project(self.state, path)
        except OSError as exc:
            QMessageBox.warning(self, "Проект", f"Не удалось сохранить проект: {exc}")
            return
        self._settings.setValue("lastProjectDir", str(path.parent))
        QMessageBox.information(self, "Проект", f"Сохранено: {path}")

    def _open_project(self) -> None:
        last = (
            self._settings.value("lastProjectDir")
            or self._settings.value("lastOutputDir")
            or str(Path.home())
        )
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть проект",
            str(last),
            "Проект Video Puzzle (*.vproj *.json);;Все файлы (*)",
        )
        if not chosen:
            return
        path = Path(chosen)
        try:
            self.state = load_project(path)
        except ProjectError as exc:
            QMessageBox.warning(self, "Проект", str(exc))
            return
        self._settings.setValue("lastProjectDir", str(path.parent))
        self._apply_state_to_widgets()

    def _preview_mosaic(self) -> None:
        if self._still_worker is not None:
            return
        dest = self._cache / "mosaic-preview.jpg"
        try:
            args = build_still_args(self.state, dest)
        except IncompleteStateError as exc:
            QMessageBox.warning(self, "Кадр склейки", str(exc))
            return
        self.preview_mosaic_btn.setEnabled(False)
        worker = StillWorker(args, dest)
        worker.succeeded.connect(self._on_still_ok)
        worker.failed.connect(self._on_still_fail)
        worker.finished.connect(self._on_still_finished)
        self._still_worker = worker
        self._track_worker(worker)
        worker.start()

    def _on_still_ok(self, image: str) -> None:
        pixmap = QPixmap(image)
        if pixmap.isNull():
            QMessageBox.warning(self, "Кадр склейки", "Не удалось открыть кадр.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Кадр склейки")
        layout = QVBoxLayout(dialog)
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scaled = pixmap.scaled(
            960,
            540,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        preview.setPixmap(scaled)
        layout.addWidget(preview)
        close = QPushButton("Закрыть")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _on_still_fail(self, message: str) -> None:
        QMessageBox.warning(self, "Кадр склейки", message)

    def _on_still_finished(self) -> None:
        self._still_worker = None
        self._sync_ui()

    def _on_file_picked(self, index: int, path: Path) -> None:
        self.state.set_slot(index, path)
        self._probe_slot(index, path)
        self._request_thumbnail(index, path, loading=True)
        self._sync_ui()

    def _on_files_dropped(self, index: int, paths: list[Path]) -> None:
        first, *rest = paths
        self.state.set_slot(index, first)
        self._probe_slot(index, first)
        self._request_thumbnail(index, first, loading=True)
        if rest:
            before = [slot.path for slot in self.state.slots]
            self.state.assign_paths(rest, start=0)
            for i, (old, new) in enumerate(
                zip(before, (slot.path for slot in self.state.slots), strict=True)
            ):
                if old != new and new is not None:
                    self._probe_slot(i, new)
                    self._request_thumbnail(i, new, loading=True)
        self._sync_ui()

    def _on_slot_cleared(self, index: int) -> None:
        self._thumb_gen[index] += 1
        self.state.clear_slot(index)
        self.canvas.slots[index].set_empty()
        self._sync_ui()

    def _on_playhead(self, seconds: float) -> None:
        self.state.set_playhead(seconds)
        self._scrub_timer.start()

    def _probe_slot(self, index: int, path: Path) -> None:
        worker = ProbeWorker(index, path)
        worker.succeeded.connect(self._on_probe_ok)
        worker.failed.connect(self._on_probe_fail)
        self._track_worker(worker)
        worker.start()

    def _on_probe_ok(self, index: int, duration: float, has_audio: bool, fps: float = 0.0) -> None:
        if self.state.slots[index].path is None:
            return
        self.state.set_probe(index, duration, has_audio, fps if fps > 0 else None)
        if self.state.playhead == 0.0:
            overlap = self.state.overlap_duration()
            if overlap is not None and overlap > 1.0:
                self.state.set_playhead(min(1.0, overlap / 2))
        self._sync_ui()
        path = self.state.slots[index].path
        if path is not None:
            self._request_thumbnail(index, path, loading=False)

    def _on_probe_fail(self, index: int, message: str) -> None:
        path = self.state.slots[index].path
        if path is None:
            return
        self.canvas.slots[index].set_error(path, message)

    def _request_thumbnail(self, index: int, path: Path, *, loading: bool) -> None:
        self._thumb_gen[index] += 1
        generation = self._thumb_gen[index]
        if loading:
            self.canvas.slots[index].set_loading(path)
        dest = self._cache / f"slot-{index}-{generation}.jpg"
        at = self.state.file_time(index)
        worker = ThumbnailWorker(index, generation, path, dest, at)
        worker.succeeded.connect(self._on_thumb_ok)
        worker.failed.connect(self._on_thumb_fail)
        self._track_worker(worker)
        worker.start()

    def _refresh_thumbnail(self, index: int, *, loading: bool) -> None:
        path = self.state.slots[index].path
        if path is None:
            return
        self._request_thumbnail(index, path, loading=loading)

    def _refresh_all_thumbnails(self) -> None:
        for index, slot in enumerate(self.state.active_slots()):
            if slot.path is not None:
                self._request_thumbnail(index, slot.path, loading=False)

    def _on_thumb_ok(self, index: int, generation: int, image: str) -> None:
        if generation != self._thumb_gen[index]:
            return
        path = self.state.slots[index].path
        if path is None:
            return
        self.canvas.slots[index].set_thumbnail(path, Path(image))

    def _on_thumb_fail(self, index: int, generation: int, message: str) -> None:
        if generation != self._thumb_gen[index]:
            return
        path = self.state.slots[index].path
        if path is None:
            return
        self.canvas.slots[index].set_error(path, message)

    def _sync_audio(self) -> None:
        if self.state.missing_slot_indexes():
            QMessageBox.information(
                self, "Синхронизация", "Сначала выберите все видео текущей схемы."
            )
            return
        self.sync_btn.setEnabled(False)
        self.align_note.setText("Считаю сдвиг по звуку…")
        worker = SyncWorker(self.state)
        worker.succeeded.connect(self._on_sync_ok)
        worker.failed.connect(self._on_sync_fail)
        self._track_worker(worker)
        worker.start()

    def _on_sync_ok(self, result: object) -> None:
        self.sync_btn.setEnabled(True)
        assert isinstance(result, SyncResult)
        apply_sync(self.state, result)
        self.align_note.setText(result.summary)
        self._sync_ui()
        self._refresh_all_thumbnails()

    def _on_sync_fail(self, message: str) -> None:
        self.sync_btn.setEnabled(True)
        self.align_note.setText(message)
        QMessageBox.warning(self, "Синхронизация", message)

    def _sync_ui(self) -> None:
        missing = self.state.missing_slot_indexes()
        running = self._proc is not None
        ready = self.state.is_complete() and not running
        probed = all(slot.duration is not None for slot in self.state.active_slots())
        self.save_btn.setEnabled(ready)
        self.render_btn.setEnabled(ready)
        self.sync_btn.setEnabled(ready)
        self.auto_frag_btn.setEnabled(self.state.is_wall and ready and probed)
        self.cancel_btn.setEnabled(running)
        self.cancel_progress_btn.setEnabled(running)
        self.cancel_progress_btn.setVisible(running)
        self.progress.setVisible(running)
        self.timeline.set_duration(self.state.overlap_duration())
        self.timeline.set_position(self.state.playhead)
        self._sync_range_widgets()
        self._update_slot_badges()
        self._sync_audio_combo()
        self.preview_mosaic_btn.setEnabled(ready and self._still_worker is None)
        if missing:
            n = len(missing)
            self.status.setText(f"Нужно выбрать ещё {n} {_plural_files(n)}.")
            self.script_view.setPlainText("")
            return
        out_w, out_h = mosaic_output_size(self.state)
        extra = ""
        spread = self.state.duration_spread()
        if spread is not None and spread >= 0.2:
            extra = f" · длительности отличаются на {spread:.1f} с"
        fragment = ""
        if self.state.is_wall:
            wall_len = self.state.wall_output_duration()
            if wall_len is not None:
                fragment = f" · стена {wall_len:.2f} с"
        elif self.state.range_enabled:
            start = self.state.resolved_range_start()
            end = self.state.resolved_range_end()
            if end is not None:
                fragment = f" · фрагмент {start:.2f}–{end:.2f} с"
        size_note = ""
        duration = self.state.export_duration()
        if duration:
            nbytes = estimate_output_bytes(
                width=out_w,
                height=out_h,
                fps=self.state.output_fps(),
                duration=duration,
                quality=self.state.quality,
                has_audio=self.state.audio_input_index() is not None,
            )
            size_note = f" · ~{format_size(nbytes)}"
        if running:
            self.status.setText("Идёт сборка видео… Можно остановить.")
        else:
            self.status.setText(
                f"Готово · выход {out_w}×{out_h} · {_quality_status(self.state.quality)}"
                f" · {self.state.output_fps():.2f} fps{size_note}{extra}{fragment}"
            )
        self.script_view.setPlainText(
            render_shell_script(self.state, Path("mosaic.mp4"), available_encoders=self._encoders)
        )

    def _sync_audio_combo(self) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("Авто (первый со звуком)", None)
        for index, slot in enumerate(self.state.active_slots()):
            if slot.path is None:
                continue
            label = f"Слот {index + 1} — {slot.path.name}"
            if not slot.has_audio:
                label += " (нет звука)"
            self.audio_combo.addItem(label, index)
        target = self.state.audio_slot
        found = 0
        if target is not None:
            for i in range(self.audio_combo.count()):
                if self.audio_combo.itemData(i) == target:
                    found = i
                    break
        self.audio_combo.setCurrentIndex(found)
        self.audio_combo.setEnabled(self.state.include_audio)
        self.normalize_check.blockSignals(True)
        self.normalize_check.setChecked(self.state.normalize_audio)
        self.normalize_check.blockSignals(False)
        self.normalize_check.setEnabled(self.state.include_audio)
        self.audio_combo.blockSignals(False)

    def _pick_output(self) -> Path | None:
        last = self._settings.value("lastOutputDir") or str(Path.home())
        suggested = str(Path(str(last)) / "mosaic.mp4")
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Куда сохранить готовое видео",
            suggested,
            "Видео (*.mp4);;Все файлы (*)",
        )
        if not chosen:
            return None
        output = Path(chosen)
        if output.suffix.lower() != ".mp4":
            output = output.with_suffix(".mp4")
        self._settings.setValue("lastOutputDir", str(output.parent))
        return output

    def _save_script(self) -> None:
        if not self._confirm_export():
            return
        output = self._pick_output()
        if output is None:
            return
        script_path = output.with_suffix(".sh")
        try:
            text = render_shell_script(self.state, output, available_encoders=self._encoders)
        except IncompleteStateError as exc:
            QMessageBox.warning(self, "Не хватает файлов", str(exc))
            return
        script_path.write_text(text, encoding="utf-8")
        os.chmod(script_path, 0o755)
        QMessageBox.information(
            self,
            "Скрипт сохранён",
            f"Скрипт: {script_path}\nВыходной файл в команде: {output}\n\nЗапуск: {script_path}",
        )

    def _render(self) -> None:
        if not self._confirm_export():
            return
        output = self._pick_output()
        if output is None:
            return
        out_w, out_h = mosaic_output_size(self.state)
        duration = self.state.export_duration() or 0.0
        if duration > 0:
            nbytes = estimate_output_bytes(
                width=out_w,
                height=out_h,
                fps=self.state.output_fps(),
                duration=duration,
                quality=self.state.quality,
                has_audio=self.state.audio_input_index() is not None,
            )
            answer = QMessageBox.question(
                self,
                "Сборка",
                f"Выход {out_w}×{out_h}, примерно {format_size(nbytes)}.\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            args = build_ffmpeg_args(
                self.state,
                output,
                overwrite=True,
                progress=True,
                available_encoders=self._encoders,
            )
        except IncompleteStateError as exc:
            QMessageBox.warning(self, "Не хватает файлов", str(exc))
            return
        self._render_total = self.state.export_duration() or 0.0
        self._render_cancelled = False
        self._render_output = output
        self._render_log = ""
        self._progress_buf = ""
        if self._render_total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(0)
        else:
            self.progress.setRange(0, 0)
        proc = QProcess(self)
        self._proc = proc
        proc.readyReadStandardOutput.connect(self._on_ffmpeg_stdout)
        proc.readyReadStandardError.connect(self._on_ffmpeg_stderr)
        proc.finished.connect(self._on_ffmpeg_finished)
        proc.start(args[0], args[1:])
        if not proc.waitForStarted(3000):
            self._proc = None
            self._render_output = None
            self._sync_ui()
            QMessageBox.warning(self, "ffmpeg", "Не удалось запустить ffmpeg.")
            return
        self._sync_ui()

    def _cancel_render(self) -> None:
        if self._proc is None:
            return
        self._render_cancelled = True
        self.cancel_btn.setEnabled(False)
        self.cancel_progress_btn.setEnabled(False)
        self._proc.terminate()
        QTimer.singleShot(1500, self._kill_if_still_running)

    def _kill_if_still_running(self) -> None:
        if self._proc is None:
            return
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            self._proc.kill()

    def _on_ffmpeg_stdout(self) -> None:
        if self._proc is None:
            return
        text = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        chunk = self._progress_buf + text
        parts = chunk.split("\n")
        self._progress_buf = parts[-1]
        for line in parts[:-1]:
            current = parse_progress_seconds(line)
            if current is None or self._render_total <= 0:
                continue
            self.progress.setValue(int(progress_fraction(current, self._render_total) * 1000))

    def _on_ffmpeg_stderr(self) -> None:
        if self._proc is None:
            return
        text = bytes(self._proc.readAllStandardError()).decode("utf-8", errors="replace")
        self._render_log += text
        if len(self._render_log) > 32_000:
            self._render_log = self._render_log[-16_000:]

    def _on_ffmpeg_finished(self, code: int, _status) -> None:
        cancelled = self._render_cancelled
        output = self._render_output
        log = self._render_log
        self._proc = None
        self._render_cancelled = False
        self._render_output = None
        self._render_log = ""
        self._progress_buf = ""
        self._sync_ui()
        if self._closing:
            return
        outcome = render_outcome(cancelled=cancelled, code=code)
        if outcome == "cancelled":
            if output is not None and should_delete_partial_output(cancelled=True, code=code):
                output.unlink(missing_ok=True)
            QMessageBox.information(self, "Сборка остановлена", "Сборка видео прервана.")
            return
        if outcome == "success":
            box = QMessageBox(self)
            box.setWindowTitle("Готово")
            box.setText("Видео собрано.")
            open_file = box.addButton("Открыть файл", QMessageBox.ButtonRole.AcceptRole)
            open_folder = box.addButton("Открыть папку", QMessageBox.ButtonRole.ActionRole)
            box.addButton("Закрыть", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if output is not None and clicked is open_file:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))
            elif output is not None and clicked is open_folder:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(output.parent)))
            return
        err = log.strip()
        message = err or f"ffmpeg завершился с кодом {code}"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("ffmpeg")
        box.setText(message if len(message) < 400 else f"ffmpeg завершился с кодом {code}")
        if err:
            box.setDetailedText(err[-4000:])
        box.exec()

    def _track_worker(self, worker: QThread) -> None:
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w) if w in self._workers else None
        )
        self._workers.append(worker)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("splitter", self.splitter.saveState())
        self._settings.setValue("quality", str(self.state.quality))
        self._settings.setValue("encoder", str(self.state.encoder))
        self._settings.setValue("cellGap", self.state.cell_gap)
        if self._proc is not None:
            self._render_cancelled = True
            self._proc.kill()
            self._proc.waitForFinished(500)
        if self._still_worker is not None:
            self._still_worker.wait(200)
        for worker in list(self._workers):
            worker.wait(200)
        super().closeEvent(event)


def _quality_status(quality: EncodeQuality) -> str:
    return {
        EncodeQuality.DRAFT: "быстрое",
        EncodeQuality.STANDARD: "обычное",
        EncodeQuality.HIGH: "высокое",
        EncodeQuality.ORIGINAL: "как оригинал",
    }[quality]


def _plural_files(n: int) -> str:
    if n == 1:
        return "файл"
    if n < 5:
        return "файла"
    return "файлов"
