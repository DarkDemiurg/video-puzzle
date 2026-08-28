from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QProcess, QStandardPaths, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from video_puzzle.ffmpeg_script import IncompleteStateError, build_ffmpeg_args, render_shell_script
from video_puzzle.layout import (
    MAX_SLOTS,
    RESOLUTIONS,
    AppMode,
    Layout,
    canvas_size,
    grid_output_size,
    output_size,
)
from video_puzzle.probe import ProbeError, probe_video
from video_puzzle.progress import (
    format_timecode,
    parse_progress_seconds,
    progress_fraction,
    render_outcome,
    should_delete_partial_output,
)
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
    succeeded = Signal(int, float, bool)
    failed = Signal(int, str)

    def __init__(self, index: int, video: Path) -> None:
        super().__init__()
        self._index = index
        self._video = video

    def run(self) -> None:
        try:
            result = probe_video(self._video)
            self.succeeded.emit(self._index, result.duration, result.has_audio)
        except ProbeError as exc:
            self.failed.emit(self._index, str(exc))


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
        self.resize(1180, 780)
        self.state = AppState()
        self._thumb_gen = [0] * MAX_SLOTS
        self._workers: list[QThread] = []
        self._proc: QProcess | None = None
        self._render_total = 0.0
        self._render_cancelled = False
        self._render_output: Path | None = None
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

        root.addWidget(self._build_sidebar())
        right = QVBoxLayout()
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
        root.addLayout(right, 1)

        for slot in self.canvas.slots:
            self._bind_slot(slot)

        self._sync_ui()

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(300)
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
            scheme_layout.addWidget(button)

        two_wrap = QWidget()
        two_layout = QVBoxLayout(two_wrap)
        two_layout.setContentsMargins(18, 0, 0, 0)
        two_label = QLabel("Для двух файлов")
        two_label.setObjectName("hint")
        self.radio_h = QRadioButton("Горизонтально")
        self.radio_v = QRadioButton("Вертикально")
        self.radio_h.setChecked(True)
        self.orient_group = QButtonGroup(self)
        self.orient_group.addButton(self.radio_h)
        self.orient_group.addButton(self.radio_v)
        two_layout.addWidget(two_label)
        two_layout.addWidget(self.radio_h)
        two_layout.addWidget(self.radio_v)
        self.radio_h.setEnabled(False)
        self.radio_v.setEnabled(False)
        scheme_layout.addWidget(two_wrap)

        pyramid_wrap = QWidget()
        pyramid_layout = QVBoxLayout(pyramid_wrap)
        pyramid_layout.setContentsMargins(18, 0, 0, 0)
        pyramid_label = QLabel("Для трёх файлов")
        pyramid_label.setObjectName("hint")
        self.radio_pyramid_wide = QRadioButton("Широкая пирамида")
        self.radio_pyramid_narrow = QRadioButton("Узкая пирамида")
        self.radio_pyramid_wide.setChecked(True)
        self.pyramid_group = QButtonGroup(self)
        self.pyramid_group.addButton(self.radio_pyramid_wide)
        self.pyramid_group.addButton(self.radio_pyramid_narrow)
        pyramid_layout.addWidget(pyramid_label)
        pyramid_layout.addWidget(self.radio_pyramid_wide)
        pyramid_layout.addWidget(self.radio_pyramid_narrow)
        self.radio_pyramid_wide.setEnabled(False)
        self.radio_pyramid_narrow.setEnabled(False)
        scheme_layout.addWidget(pyramid_wrap)
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

        res = QGroupBox("Разрешение выхода")
        res_layout = QVBoxLayout(res)
        self.res_group = QButtonGroup(self)
        self.res_buttons: dict[int, QRadioButton] = {}
        for height, (width, _) in RESOLUTIONS.items():
            button = QRadioButton(f"{height}p  ({width}×{height})")
            self.res_group.addButton(button)
            self.res_buttons[height] = button
            res_layout.addWidget(button)
        self.res_buttons[1080].setChecked(True)
        layout.addWidget(res)

        self.audio_check = QCheckBox("Звук с первого файла, у которого он есть")
        self.audio_check.setChecked(True)
        self.audio_check.toggled.connect(self._on_audio_toggled)
        layout.addWidget(self.audio_check)

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
        return sidebar

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

    def _on_mode_changed(self) -> None:
        wall = self.radio_wall.isChecked()
        self.state.set_mode(AppMode.WALL if wall else AppMode.PUZZLE)
        self.scheme_box.setVisible(not wall)
        self.wall_box.setVisible(wall)
        self.puzzle_extras.setVisible(not wall)
        self.wall_hint.setVisible(wall)
        if wall:
            created = self.canvas.ensure_slots(self.state.active_count)
            for widget in created:
                self._bind_slot(widget)
            self.canvas.apply_grid(self.state.wall_rows, self.state.wall_cols)
        else:
            self.canvas.apply_layout(self.state.layout)
        self.canvas.set_wall_mode(wall)
        self._sync_ui()
        self._refresh_all_thumbnails()

    def _on_wall_grid(self) -> None:
        if not self.state.is_wall:
            return
        self.state.set_wall_grid(self.rows_spin.value(), self.cols_spin.value())
        created = self.canvas.ensure_slots(self.state.active_count)
        for widget in created:
            self._bind_slot(widget)
        self.canvas.apply_grid(self.state.wall_rows, self.state.wall_cols)
        self.canvas.set_wall_mode(True)
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
        self._refresh_thumbnail(index, loading=False)
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

    def _on_scheme_changed(self) -> None:
        two = self.radio_2.isChecked()
        three = self.radio_3.isChecked()
        self.radio_h.setEnabled(two)
        self.radio_v.setEnabled(two)
        self.radio_pyramid_wide.setEnabled(three)
        self.radio_pyramid_narrow.setEnabled(three)
        layout = self._selected_layout()
        if layout is self.state.layout:
            return
        self.state.set_layout(layout)
        self.canvas.apply_layout(layout)
        self._sync_ui()
        self._refresh_all_thumbnails()

    def _on_resolution_changed(self) -> None:
        for height, button in self.res_buttons.items():
            if button.isChecked():
                self.state.set_resolution(height)
                self._sync_ui()
                return

    def _on_audio_toggled(self, checked: bool) -> None:
        self.state.include_audio = checked
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
        self._sync_range_widgets()
        self._sync_script_preview()

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

    def _sync_script_preview(self) -> None:
        if self.state.missing_slot_indexes():
            self.script_view.setPlainText("")
            return
        self.script_view.setPlainText(render_shell_script(self.state, Path("mosaic.mp4")))

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

    def _on_probe_ok(self, index: int, duration: float, has_audio: bool) -> None:
        if self.state.slots[index].path is None:
            return
        self.state.set_probe(index, duration, has_audio)
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
        if missing:
            n = len(missing)
            self.status.setText(f"Нужно выбрать ещё {n} {_plural_files(n)}.")
            self.script_view.setPlainText("")
            return
        canvas_w, canvas_h = canvas_size(self.state.resolution)
        if self.state.is_wall:
            out_w, out_h = grid_output_size(
                self.state.wall_cols, self.state.wall_rows, canvas_w, canvas_h
            )
        else:
            out_w, out_h = output_size(self.state.layout, canvas_w, canvas_h)
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
        if running:
            self.status.setText("Идёт сборка видео… Можно остановить.")
        else:
            self.status.setText(f"Готово · выход {out_w}×{out_h}{extra}{fragment}")
        self.script_view.setPlainText(render_shell_script(self.state, Path("mosaic.mp4")))

    def _pick_output(self) -> Path | None:
        suggested = str(Path.home() / "mosaic.mp4")
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
        return output

    def _save_script(self) -> None:
        if not self._confirm_export():
            return
        output = self._pick_output()
        if output is None:
            return
        script_path = output.with_suffix(".sh")
        try:
            text = render_shell_script(self.state, output)
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
        try:
            args = build_ffmpeg_args(self.state, output, overwrite=True, progress=True)
        except IncompleteStateError as exc:
            QMessageBox.warning(self, "Не хватает файлов", str(exc))
            return
        self._render_total = self.state.export_duration() or 0.0
        self._render_cancelled = False
        self._render_output = output
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
        for line in text.splitlines():
            current = parse_progress_seconds(line)
            if current is None or self._render_total <= 0:
                continue
            self.progress.setValue(int(progress_fraction(current, self._render_total) * 1000))

    def _on_ffmpeg_stderr(self) -> None:
        if self._proc is None:
            return
        self._proc.readAllStandardError()

    def _on_ffmpeg_finished(self, code: int, _status) -> None:
        proc = self._proc
        cancelled = self._render_cancelled
        output = self._render_output
        self._proc = None
        self._render_cancelled = False
        self._render_output = None
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
            QMessageBox.information(self, "Готово", "Видео собрано.")
            return
        err = ""
        if proc is not None:
            err = bytes(proc.readAllStandardError()).decode("utf-8", errors="replace").strip()
        QMessageBox.warning(self, "ffmpeg", err or f"ffmpeg завершился с кодом {code}")

    def _track_worker(self, worker: QThread) -> None:
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w) if w in self._workers else None
        )
        self._workers.append(worker)

    def closeEvent(self, event) -> None:
        self._closing = True
        if self._proc is not None:
            self._render_cancelled = True
            self._proc.kill()
            self._proc.waitForFinished(500)
        for worker in list(self._workers):
            worker.wait(200)
        super().closeEvent(event)


def _plural_files(n: int) -> str:
    if n == 1:
        return "файл"
    if n < 5:
        return "файла"
    return "файлов"
