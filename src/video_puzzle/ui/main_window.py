from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import (
    QLocale,
    QProcess,
    QSettings,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QPixmap, QStandardItemModel
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

from video_puzzle.cache import clear_cache, prune_cache, prune_cache_dirs
from video_puzzle.encode import (
    DEFAULT_ENCODER,
    DEFAULT_QUALITY,
    EncodeQuality,
    EncoderKind,
    estimate_output_bytes,
    format_size,
)
from video_puzzle.encoders import detect_encoders, ffmpeg_binary
from video_puzzle.ffmpeg_script import (
    IncompleteStateError,
    build_ffmpeg_args,
    build_still_args,
    mosaic_output_size,
    render_shell_script,
)
from video_puzzle.i18n import (
    available_languages,
    detect_language,
    get_language,
    set_language,
    tr,
    trn,
)
from video_puzzle.layout import MAX_SLOTS, RESOLUTIONS, AppMode, Layout, even
from video_puzzle.probe import ProbeError, probe_video
from video_puzzle.progress import (
    format_timecode,
    parse_progress_seconds,
    progress_fraction,
    render_outcome,
    should_delete_partial_output,
)
from video_puzzle.project import ProjectError, load_project, save_project, state_signature
from video_puzzle.state import AppState, Slot
from video_puzzle.sync import SyncError, SyncResult, analyze_slots, apply_sync
from video_puzzle.thumbnails import ThumbnailError, extract_thumbnail
from video_puzzle.ui.preview_canvas import PreviewCanvas
from video_puzzle.ui.qt_translations import apply_qt_translations
from video_puzzle.ui.timeline import TimelineBar
from video_puzzle.ui.trim_editor import TrimEditor
from video_puzzle.wall import AutoFragmentError, apply_shortest_from_start, inspect_wall

# A single still frame from a full wall decode: generous, but it must not hang close().
STILL_TIMEOUT_SECONDS = 120.0
WORKER_SHUTDOWN_SECONDS = 3.0
LOGGER = logging.getLogger(__name__)

_QUALITY_LABELS = {
    EncodeQuality.DRAFT: "Draft",
    EncodeQuality.STANDARD: "Standard",
    EncodeQuality.HIGH: "High",
    EncodeQuality.ORIGINAL: "Original",
}
_QUALITY_TIPS = {
    EncodeQuality.DRAFT: "Faster and smaller; more visible compression.",
    EncodeQuality.STANDARD: "Balance of size and quality (as before).",
    EncodeQuality.HIGH: "Fewer artifacts; slower render.",
    EncodeQuality.ORIGINAL: "Almost lossless after stitching; noticeably larger file.",
}
_ENCODER_LABELS = {
    EncoderKind.AUTO: "Auto (NVENC / QSV / CPU)",
    EncoderKind.CPU: "CPU (libx264)",
    EncoderKind.NVENC: "NVIDIA (NVENC)",
    EncoderKind.QSV: "Intel (QSV)",
}


class _WorkerCancelled(Exception):
    """Raised inside a worker thread when the window asked it to stop."""


class _CancelableWorker(QThread):
    """QThread with a cancellable ``subprocess.run``-compatible runner.

    ``extract_thumbnail``/``probe_video``/``detect_encoders`` all use ``runner=`` with
    the ``subprocess.run`` contract. ``subprocess.run`` itself cannot be interrupted,
    so this class drives ``Popen`` by hand: a cancel kills the child, and the thread
    can be joined before the window and its workers are destroyed.
    """

    _POLL_SECONDS = 0.1

    def __init__(self) -> None:
        super().__init__()
        self._cancelled = False
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True
        self.requestInterruption()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _throw_if_cancelled(self) -> None:
        if self._cancelled or self.isInterruptionRequested():
            raise _WorkerCancelled

    def _run(self, cmd, **kwargs):
        """Minimal ``subprocess.run`` implementation that honours ``cancel()``."""
        check = bool(kwargs.pop("check", False))
        capture_output = bool(kwargs.pop("capture_output", False))
        timeout = kwargs.pop("timeout", None)
        if capture_output:
            kwargs.setdefault("stdout", subprocess.PIPE)
            kwargs.setdefault("stderr", subprocess.PIPE)
        self._throw_if_cancelled()
        process = subprocess.Popen(cmd, **kwargs)
        with self._process_lock:
            self._process = process
        if self._cancelled:
            process.kill()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=self._POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if self._cancelled or self.isInterruptionRequested():
                        process.kill()
                        process.communicate()
                        raise _WorkerCancelled from None
                    if deadline is not None and time.monotonic() >= deadline:
                        process.kill()
                        process.communicate()
                        raise subprocess.TimeoutExpired(cmd, timeout) from None
        finally:
            with self._process_lock:
                self._process = None
        result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        if check and result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, output=stdout, stderr=stderr
            )
        return result


class ThumbnailWorker(_CancelableWorker):
    succeeded = Signal(int, int, int, str, str)
    failed = Signal(int, int, int, str, str)

    def __init__(
        self,
        epoch: int,
        index: int,
        generation: int,
        video: Path,
        dest: Path,
        at_seconds: float,
    ) -> None:
        super().__init__()
        self._epoch = epoch
        self._index = index
        self._generation = generation
        self._video = video
        self._dest = dest
        self._at = at_seconds

    def run(self) -> None:
        try:
            extract_thumbnail(self._video, self._dest, at_seconds=self._at, runner=self._run)
        except _WorkerCancelled:
            return
        except ThumbnailError as exc:
            LOGGER.warning("Thumbnail failed for %s: %s", self._video, exc.detail or exc)
            if not self._cancelled:
                self.failed.emit(
                    self._epoch, self._index, self._generation, str(self._video), str(exc)
                )
            return
        except Exception:
            LOGGER.exception("Unexpected thumbnail failure for %s", self._video)
            if not self._cancelled:
                self.failed.emit(
                    self._epoch,
                    self._index,
                    self._generation,
                    str(self._video),
                    tr("Could not build the preview for {name}", name=self._video.name),
                )
            return
        if not self._cancelled:
            self.succeeded.emit(
                self._epoch, self._index, self._generation, str(self._video), str(self._dest)
            )


class ProbeWorker(_CancelableWorker):
    succeeded = Signal(int, int, str, float, bool, bool, float)
    failed = Signal(int, int, str, str)

    def __init__(self, epoch: int, index: int, video: Path) -> None:
        super().__init__()
        self._epoch = epoch
        self._index = index
        self._video = video

    def run(self) -> None:
        try:
            result = probe_video(self._video, runner=self._run)
        except _WorkerCancelled:
            return
        except ProbeError as exc:
            LOGGER.warning("Probe failed for %s: %s", self._video, exc.detail or exc)
            if not self._cancelled:
                self.failed.emit(self._epoch, self._index, str(self._video), str(exc))
            return
        except Exception:
            LOGGER.exception("Unexpected probe failure for %s", self._video)
            if not self._cancelled:
                self.failed.emit(
                    self._epoch,
                    self._index,
                    str(self._video),
                    tr("Could not read video metadata for {video}", video=self._video.name),
                )
            return
        if not self._cancelled:
            self.succeeded.emit(
                self._epoch,
                self._index,
                str(self._video),
                result.duration,
                result.has_audio,
                result.has_video,
                result.fps or 0.0,
            )


class StillWorker(_CancelableWorker):
    succeeded = Signal(int, str)
    failed = Signal(int, str)

    def __init__(self, epoch: int, args: list[str], dest: Path) -> None:
        super().__init__()
        self._epoch = epoch
        self._args = args
        self._dest = dest

    def run(self) -> None:
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            # A stale file from the previous preview would look like a successful run.
            self._dest.unlink(missing_ok=True)
            result = self._run(
                self._args,
                capture_output=True,
                text=True,
                check=False,
                timeout=STILL_TIMEOUT_SECONDS,
            )
            if result.returncode != 0 or not self._dest.is_file() or self._dest.stat().st_size == 0:
                detail = (result.stderr or result.stdout or "").strip()
                LOGGER.warning("Mosaic preview failed: %s", detail or "no output")
                if not self._cancelled:
                    self.failed.emit(self._epoch, tr("Could not build the mosaic frame"))
                return
            if not self._cancelled:
                self.succeeded.emit(self._epoch, str(self._dest))
        except _WorkerCancelled:
            return
        except subprocess.TimeoutExpired:
            if not self._cancelled:
                self.failed.emit(
                    self._epoch,
                    tr(
                        "ffmpeg did not build the frame within {seconds:.0f} s",
                        seconds=STILL_TIMEOUT_SECONDS,
                    ),
                )
        except Exception:
            LOGGER.exception("Unexpected mosaic preview failure")
            if not self._cancelled:
                self.failed.emit(self._epoch, tr("Could not build the mosaic frame"))


class _ProbeInterrupted(Exception):
    """Raised inside the probe thread when the window asked to shut it down."""


class EncoderProbeWorker(_CancelableWorker):
    """detect_encoders() starts up to three ffmpeg processes and can take tens of
    seconds when the GPU stack hangs, so it must never run in the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            names = detect_encoders(runner=self._run)
        except _WorkerCancelled:
            return
        except Exception as exc:
            if not self._cancelled:
                self.failed.emit(str(exc))
            return
        if not self._cancelled:
            self.succeeded.emit(names)


class SyncWorker(_CancelableWorker):
    succeeded = Signal(object, int)
    failed = Signal(int, str)

    def __init__(self, epoch: int, state: AppState) -> None:
        super().__init__()
        self._epoch = epoch
        self._slots = [
            Slot(path=slot.path, duration=slot.duration, has_audio=slot.has_audio)
            for slot in state.active_slots()
        ]

    def run(self) -> None:
        try:
            result = analyze_slots(self._slots, runner=self._run)
        except _WorkerCancelled:
            return
        except SyncError as exc:
            LOGGER.warning("Synchronization failed: %s", exc.detail or exc)
            if not self._cancelled:
                self.failed.emit(self._epoch, str(exc))
            return
        except Exception:
            LOGGER.exception("Unexpected synchronization failure")
            if not self._cancelled:
                self.failed.emit(self._epoch, tr("Could not synchronize the clips."))
            return
        if not self._cancelled:
            self.succeeded.emit(result, self._epoch)


def _slots_signature(slots: Sequence[Slot]) -> tuple[tuple[Path | None, float | None], ...]:
    """Identity of the active scheme as async workers see it.

    A worker result is applicable only while the signature is unchanged: replacing a
    file, swapping slots, shrinking the scheme or opening another project all change
    it, so stale probe/sync answers are dropped instead of leaking onto other clips.
    """
    return tuple((slot.path, slot.duration) for slot in slots)


def _same_file(first: Path, second: Path) -> bool:
    """True for the same file even when reached through a symlink, hardlink or ``..``.

    ffmpeg only refuses byte-identical output/input *paths*; a symlink or hardlink
    happily re-encodes the source over itself. Exact samefile/resolve checks are the
    only thing that keeps a user's original clip alive.
    """
    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:
        pass
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return False


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings()
        saved_language = self._settings.value("language")
        set_language(
            str(saved_language) if saved_language else detect_language(QLocale.system().name())
        )
        apply_qt_translations()
        self.setWindowTitle("Video Puzzle")
        self.state = AppState()
        # Until the probe thread reports, only the software encoder is known to work;
        # hardware buttons are re-enabled in _apply_encoder_availability().
        self._encoders: set[str] = {EncoderKind.CPU.value}
        self._encoders_detected = False
        self._encoder_probe: EncoderProbeWorker | None = None
        # Encoder the user asked for (settings, project, or a click). It is applied
        # only once detection says ffmpeg can open it, so the wish is kept separately.
        self._wanted_encoder: EncoderKind | None = None
        # Bumped whenever the whole state object is replaced (another project opened).
        # Workers carry the epoch they started in; a result from an older epoch would
        # write into a state that no longer exists.
        self._project_epoch = 0
        # Snapshot of the active scheme taken when a sync started; trims computed for
        # one arrangement of clips must not be applied to another.
        self._sync_signature: tuple[tuple[Path | None, float | None], ...] | None = None
        # File the state came from and a snapshot of what was written there. Comparing
        # the live state against the snapshot is what lets "Open project" warn instead
        # of silently throwing away edits that were never saved.
        self._project_path: Path | None = None
        self._saved_signature = state_signature(self.state, Path.home())
        self._thumb_gen = [0] * MAX_SLOTS
        self._workers: list[QThread] = []
        self._proc: QProcess | None = None
        self._still_worker: StillWorker | None = None
        self._render_total = 0.0
        self._render_cancelled = False
        self._render_output: Path | None = None
        # Whether the output file already existed before ffmpeg was started: only a
        # file we created ourselves may be cleaned up after a failed or cancelled run.
        self._render_preexisting = False
        self._render_log = ""
        self._progress_buf = ""
        self._closing = False
        # Resolve ffmpeg once: a GUI app's PATH is often shorter than the shell's, and a
        # bare "ffmpeg" handed to QProcess then fails with only a generic message.
        self._ffmpeg = ffmpeg_binary()
        cache_base = (
            Path(
                QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
                or tempfile.gettempdir()
            )
            / "video-puzzle"
        )
        # Per-process directory: a second instance used to wipe the first one's frames
        # on startup, and both wrote the same "mosaic-preview.jpg".
        self._cache = cache_base / f"{os.getpid()}-{uuid.uuid4().hex}"
        self._cache.mkdir(parents=True, exist_ok=True)
        prune_cache_dirs(cache_base, keep=self._cache)
        # Frames written during a scrub are referenced only while they are shown, so
        # leftovers from a previous run are dead weight and the cache is wiped on start.
        clear_cache(self._cache)
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.setInterval(120)
        self._scrub_timer.timeout.connect(self._refresh_all_thumbnails)
        self._cache_prune_timer = QTimer(self)
        self._cache_prune_timer.setInterval(60_000)
        self._cache_prune_timer.timeout.connect(lambda: prune_cache(self._cache))
        self._cache_prune_timer.start()

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
            tr(
                "Click the timeline to set one moment in every preview. "
                "The output range is set on the same timeline, for example from 20 to 60 seconds."
            )
        )
        self.timeline_hint.setObjectName("hint")
        self.timeline_hint.setWordWrap(True)
        puzzle_layout.addWidget(self.timeline_hint)
        self.timeline = TimelineBar()
        self.timeline.position_chosen.connect(self._on_playhead)
        puzzle_layout.addWidget(self.timeline)
        puzzle_layout.addWidget(self._build_range_row())
        align_row = QHBoxLayout()
        self.sync_btn = QPushButton(tr("Synchronize by audio"))
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
            tr(
                "Click a cell to mark a fragment (I/O, like in an editor). "
                '"Shortest fragments" sets every clip\'s start to the beginning and its '
                "length to the shortest file. The preview shows the first frame of the "
                "fragment; the length is shown on the cell."
            )
        )
        self.wall_hint.setObjectName("hint")
        self.wall_hint.setWordWrap(True)
        self.wall_hint.setVisible(False)
        right.addWidget(self.wall_hint)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.cancel_progress_btn = QPushButton(tr("Stop"))
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
            tr("The ffmpeg command will appear here when every cell is filled.")
        )
        self.script_view.setFixedHeight(120)
        right.addWidget(self.script_view)
        extra_row = QHBoxLayout()
        self.preview_mosaic_btn = QPushButton(tr("Mosaic preview"))
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
        self._apply_encoder_selection()
        self._retranslate_ui()
        # The baseline for "unsaved changes" is taken after settings and codec
        # resolution: before them the state on screen is not what the program settled
        # on, and every window would open already looking dirty.
        self._mark_saved()
        self._sync_ui()
        self._start_encoder_probe()
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
        self.sidebar_hint = QLabel(
            tr("Puzzle — 2–4 clips. Video wall — a grid with its own fragment in every cell.")
        )
        self.sidebar_hint.setObjectName("hint")
        self.sidebar_hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.sidebar_hint)

        language_row = QHBoxLayout()
        self.language_label = QLabel(tr("Language"))
        self.language_label.setObjectName("hint")
        self.language_combo = QComboBox()
        for code, name in available_languages().items():
            self.language_combo.addItem(name, code)
        current_language = self.language_combo.findData(get_language())
        if current_language >= 0:
            self.language_combo.setCurrentIndex(current_language)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        language_row.addWidget(self.language_label)
        language_row.addWidget(self.language_combo, 1)
        layout.addLayout(language_row)

        project_row = QHBoxLayout()
        self.open_project_btn = QPushButton(tr("Open project…"))
        self.open_project_btn.setObjectName("secondary")
        self.save_project_btn = QPushButton(tr("Save project…"))
        self.save_project_btn.setObjectName("secondary")
        self.open_project_btn.clicked.connect(self._open_project)
        self.save_project_btn.clicked.connect(lambda: self._save_project())
        project_row.addWidget(self.open_project_btn)
        project_row.addWidget(self.save_project_btn)
        layout.addLayout(project_row)

        self.mode_box = QGroupBox(tr("Mode"))
        mode_layout = QVBoxLayout(self.mode_box)
        self.radio_puzzle = QRadioButton(tr("Puzzle"))
        self.radio_wall = QRadioButton(tr("Video wall"))
        self.radio_puzzle.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.radio_puzzle)
        self.mode_group.addButton(self.radio_wall)
        mode_layout.addWidget(self.radio_puzzle)
        mode_layout.addWidget(self.radio_wall)
        self.radio_puzzle.toggled.connect(self._on_mode_changed)
        layout.addWidget(self.mode_box)

        self.scheme_box = QGroupBox(tr("Mosaic layout"))
        scheme_layout = QVBoxLayout(self.scheme_box)
        self.radio_2 = QRadioButton(tr("2 files"))
        self.radio_3 = QRadioButton(tr("3 files — pyramid"))
        self.radio_4 = QRadioButton(tr("4 files — square"))
        self.radio_4.setChecked(True)
        self.count_group = QButtonGroup(self)
        for button in (self.radio_2, self.radio_3, self.radio_4):
            self.count_group.addButton(button)

        self.two_wrap = QWidget()
        two_layout = QVBoxLayout(self.two_wrap)
        two_layout.setContentsMargins(18, 0, 0, 0)
        self.radio_h = QRadioButton(tr("Horizontal"))
        self.radio_v = QRadioButton(tr("Vertical"))
        self.radio_h.setChecked(True)
        self.orient_group = QButtonGroup(self)
        self.orient_group.addButton(self.radio_h)
        self.orient_group.addButton(self.radio_v)
        two_layout.addWidget(self.radio_h)
        two_layout.addWidget(self.radio_v)

        self.pyramid_wrap = QWidget()
        pyramid_layout = QVBoxLayout(self.pyramid_wrap)
        pyramid_layout.setContentsMargins(18, 0, 0, 0)
        self.radio_pyramid_wide = QRadioButton(tr("Wide pyramid"))
        self.radio_pyramid_narrow = QRadioButton(tr("Narrow pyramid"))
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

        self.wall_box = QGroupBox(tr("Video wall grid"))
        wall_layout = QVBoxLayout(self.wall_box)
        grid_row = QHBoxLayout()
        self.rows_spin = QSpinBox()
        self.cols_spin = QSpinBox()
        self.rows_caption = QLabel(tr("Rows"))
        self.cols_caption = QLabel(tr("Columns"))
        for spin, caption in (
            (self.rows_spin, self.rows_caption),
            (self.cols_spin, self.cols_caption),
        ):
            spin.setRange(1, 8)
            spin.setValue(2)
            wrap = QVBoxLayout()
            caption.setObjectName("hint")
            wrap.addWidget(caption)
            wrap.addWidget(spin)
            grid_row.addLayout(wrap)
        self.rows_spin.valueChanged.connect(self._on_wall_grid)
        self.cols_spin.valueChanged.connect(self._on_wall_grid)
        wall_layout.addLayout(grid_row)
        self.auto_frag_btn = QPushButton(tr("Shortest fragments"))
        self.auto_frag_btn.setObjectName("secondary")
        self.auto_frag_btn.setToolTip(
            tr("Every clip: start at the beginning, length of the shortest file.")
        )
        self.auto_frag_btn.clicked.connect(self._auto_wall_fragments)
        wall_layout.addWidget(self.auto_frag_btn)
        self.wall_box.setVisible(False)
        layout.addWidget(self.wall_box)

        out_row = QHBoxLayout()
        self.res_box = QGroupBox(tr("Resolution"))
        res_layout = QVBoxLayout(self.res_box)
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
        out_row.addWidget(self.res_box)

        self.quality_box = QGroupBox(tr("Quality"))
        quality_layout = QVBoxLayout(self.quality_box)
        self.quality_group = QButtonGroup(self)
        self.quality_buttons: dict[EncodeQuality, QRadioButton] = {}
        for kind, label in _QUALITY_LABELS.items():
            button = QRadioButton(tr(label))
            button.setToolTip(tr(_QUALITY_TIPS[kind]))
            self.quality_group.addButton(button)
            self.quality_buttons[kind] = button
            quality_layout.addWidget(button)
        self.quality_buttons[DEFAULT_QUALITY].setChecked(True)
        out_row.addWidget(self.quality_box)
        layout.addLayout(out_row)

        self.encoder_box = QGroupBox(tr("Codec"))
        encoder_layout = QVBoxLayout(self.encoder_box)
        self.encoder_group = QButtonGroup(self)
        self.encoder_buttons: dict[EncoderKind, QRadioButton] = {}
        for encoder_kind, label in _ENCODER_LABELS.items():
            button = QRadioButton(tr(label))
            self.encoder_group.addButton(button)
            self.encoder_buttons[encoder_kind] = button
            encoder_layout.addWidget(button)
        self.encoder_buttons[DEFAULT_ENCODER].setChecked(True)
        self._apply_encoder_availability()
        layout.addWidget(self.encoder_box)

        self.gap_box = QGroupBox(tr("Gap between cells"))
        gap_layout = QHBoxLayout(self.gap_box)
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 40)
        self.gap_spin.setSingleStep(2)
        self.gap_spin.setSuffix(tr(" px"))
        self.gap_spin.valueChanged.connect(self._on_gap_changed)
        gap_layout.addWidget(self.gap_spin)
        layout.addWidget(self.gap_box)

        self.audio_check = QCheckBox(tr("Include audio"))
        self.audio_check.setChecked(True)
        self.audio_check.toggled.connect(self._on_audio_toggled)
        layout.addWidget(self.audio_check)
        self.audio_combo = QComboBox()
        self.audio_combo.currentIndexChanged.connect(self._on_audio_slot_changed)
        layout.addWidget(self.audio_combo)
        self.normalize_check = QCheckBox(tr("Normalize volume"))
        self.normalize_check.toggled.connect(self._on_normalize_toggled)
        layout.addWidget(self.normalize_check)

        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.save_btn = QPushButton(tr("Save script…"))
        self.save_btn.setObjectName("secondary")
        self.save_btn.clicked.connect(self._save_script)
        self.render_btn = QPushButton(tr("Render video…"))
        self.render_btn.setObjectName("primary")
        self.render_btn.clicked.connect(self._render)
        self.cancel_btn = QPushButton(tr("Stop render"))
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
        self.range_check = QCheckBox(tr("Output range"))
        self.range_check.toggled.connect(self._on_range_toggled)
        self.range_start = QDoubleSpinBox()
        self.range_end = QDoubleSpinBox()
        for spin in (self.range_start, self.range_end):
            spin.setDecimals(2)
            spin.setRange(0.0, 86400.0)
            spin.setSuffix(tr(" s"))
            spin.setSingleStep(1.0)
            spin.setEnabled(False)
            spin.valueChanged.connect(self._on_range_spins)
        self.mark_start_btn = QPushButton(tr("Start at cursor"))
        self.mark_end_btn = QPushButton(tr("End at cursor"))
        for button in (self.mark_start_btn, self.mark_end_btn):
            button.setObjectName("secondary")
            button.setEnabled(False)
        self.mark_start_btn.clicked.connect(self._mark_start)
        self.mark_end_btn.clicked.connect(self._mark_end)
        self.range_from_label = QLabel(tr("from"))
        self.range_to_label = QLabel(tr("to"))
        layout.addWidget(self.range_check)
        layout.addWidget(self.range_from_label)
        layout.addWidget(self.range_start)
        layout.addWidget(self.mark_start_btn)
        layout.addWidget(self.range_to_label)
        layout.addWidget(self.range_end)
        layout.addWidget(self.mark_end_btn)
        layout.addStretch(1)
        return row

    def _on_language_changed(self, index: int) -> None:
        code = self.language_combo.itemData(index)
        if not isinstance(code, str) or code == get_language():
            return
        set_language(code)
        self._settings.setValue("language", code)
        apply_qt_translations()
        self._retranslate_ui()

    def _retranslate_ui(self) -> None:
        """Apply the current language to every widget that is already on screen."""
        self.language_combo.blockSignals(True)
        current_language = self.language_combo.findData(get_language())
        if current_language >= 0:
            self.language_combo.setCurrentIndex(current_language)
        self.language_combo.blockSignals(False)
        self.language_label.setText(tr("Language"))
        self.sidebar_hint.setText(
            tr("Puzzle — 2–4 clips. Video wall — a grid with its own fragment in every cell.")
        )
        self.open_project_btn.setText(tr("Open project…"))
        self.save_project_btn.setText(tr("Save project…"))
        self.mode_box.setTitle(tr("Mode"))
        self.radio_puzzle.setText(tr("Puzzle"))
        self.radio_wall.setText(tr("Video wall"))
        self.scheme_box.setTitle(tr("Mosaic layout"))
        self.radio_2.setText(tr("2 files"))
        self.radio_3.setText(tr("3 files — pyramid"))
        self.radio_4.setText(tr("4 files — square"))
        self.radio_h.setText(tr("Horizontal"))
        self.radio_v.setText(tr("Vertical"))
        self.radio_pyramid_wide.setText(tr("Wide pyramid"))
        self.radio_pyramid_narrow.setText(tr("Narrow pyramid"))
        self.wall_box.setTitle(tr("Video wall grid"))
        self.rows_caption.setText(tr("Rows"))
        self.cols_caption.setText(tr("Columns"))
        self.auto_frag_btn.setText(tr("Shortest fragments"))
        self.auto_frag_btn.setToolTip(
            tr("Every clip: start at the beginning, length of the shortest file.")
        )
        self.res_box.setTitle(tr("Resolution"))
        self.quality_box.setTitle(tr("Quality"))
        for kind, label in _QUALITY_LABELS.items():
            self.quality_buttons[kind].setText(tr(label))
            self.quality_buttons[kind].setToolTip(tr(_QUALITY_TIPS[kind]))
        self.encoder_box.setTitle(tr("Codec"))
        for encoder_kind, label in _ENCODER_LABELS.items():
            self.encoder_buttons[encoder_kind].setText(tr(label))
        self.gap_box.setTitle(tr("Gap between cells"))
        self.gap_spin.setSuffix(tr(" px"))
        self.audio_check.setText(tr("Include audio"))
        self.normalize_check.setText(tr("Normalize volume"))
        self.save_btn.setText(tr("Save script…"))
        self.render_btn.setText(tr("Render video…"))
        self.cancel_btn.setText(tr("Stop render"))
        self.timeline_hint.setText(
            tr(
                "Click the timeline to set one moment in every preview. "
                "The output range is set on the same timeline, for example from 20 to 60 seconds."
            )
        )
        self.wall_hint.setText(
            tr(
                "Click a cell to mark a fragment (I/O, like in an editor). "
                '"Shortest fragments" sets every clip\'s start to the beginning and its '
                "length to the shortest file. The preview shows the first frame of the "
                "fragment; the length is shown on the cell."
            )
        )
        self.sync_btn.setText(tr("Synchronize by audio"))
        self.cancel_progress_btn.setText(tr("Stop"))
        self.preview_mosaic_btn.setText(tr("Mosaic preview"))
        self.script_view.setPlaceholderText(
            tr("The ffmpeg command will appear here when every cell is filled.")
        )
        self.range_check.setText(tr("Output range"))
        self.mark_start_btn.setText(tr("Start at cursor"))
        self.mark_end_btn.setText(tr("End at cursor"))
        self.range_from_label.setText(tr("from"))
        self.range_to_label.setText(tr("to"))
        self.range_start.setSuffix(tr(" s"))
        self.range_end.setSuffix(tr(" s"))
        self._apply_encoder_availability()
        for slot in self.canvas.slots:
            slot.retranslate()
        self.timeline.retranslate()
        self._sync_ui()

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
        gap = even(self.state.cell_gap)
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
        self._restore_widget_cells()
        self._sync_ui()
        self._refresh_all_thumbnails()

    def _on_wall_grid(self) -> None:
        if not self.state.is_wall:
            return
        self.state.set_wall_grid(self.rows_spin.value(), self.cols_spin.value())
        self._apply_canvas_layout()
        self._restore_widget_cells()
        self._sync_ui()

    def _restore_widget_cells(self) -> None:
        """Make visible cells match the state without re-probing intact ones.

        Shrinking a wall used to blank the hidden widgets while the state kept their
        files; growing it again left the cells empty although ``is_ready_for_export``
        said True. Comparing ``current_path`` restores only the cells that really lost
        their preview.
        """
        count = self.state.active_count
        for index in range(min(count, len(self.canvas.slots))):
            slot = self.state.slots[index]
            widget = self.canvas.slots[index]
            if slot.path is None:
                widget.set_empty()
            elif widget.current_path != slot.path:
                self._probe_slot(index, slot.path)
                self._request_thumbnail(index, slot.path, loading=True)

    def _open_trim_editor(self, index: int) -> None:
        slot = self.state.slots[index]
        if slot.path is None:
            return
        if slot.duration is None:
            QMessageBox.information(
                self,
                tr("Mark a fragment"),
                tr(
                    "Wait until the file duration is known; the mark range will then be "
                    "limited to the real clip length."
                ),
            )
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
            QMessageBox.warning(self, tr("Video wall"), str(exc))
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
                widget.set_fragment_label(tr("no fragment"))

    def _confirm_export(self) -> bool:
        if not self.state.is_wall:
            return True
        report = inspect_wall(self.state.slots, self.state.active_count)
        if report.missing_files:
            QMessageBox.warning(self, tr("Video wall"), tr("Fill every cell in the grid."))
            return False
        if report.missing_fragments:
            labels = ", ".join(str(i + 1) for i in report.missing_fragments)
            choice = QMessageBox.question(
                self,
                tr("No fragment"),
                tr(
                    "Slots {slots} have no fragment (I/O).\n"
                    "Open the trim editor for the first one?",
                    slots=labels,
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self._open_trim_editor(report.missing_fragments[0])
            return False
        if report.length_warning:
            assert report.min_duration is not None
            assert report.max_duration is not None
            assert report.spread is not None
            answer = QMessageBox.question(
                self,
                tr("Fragments have different lengths"),
                tr(
                    "Fragments range from {minimum:.1f} to {maximum:.1f} s "
                    "(difference {spread:.1f} s).\n"
                    "The wall will use the shortest one ({minimum:.1f} s).\n"
                    "Continue?",
                    minimum=report.min_duration,
                    maximum=report.max_duration,
                    spread=report.spread,
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
        self._restore_widget_cells()
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
                self._wanted_encoder = kind
                self.state.set_encoder(kind)
                self._settings.setValue("encoder", str(kind))
                self._sync_ui()
                return

    def _apply_encoder_availability(self) -> None:
        """Enable only the codec buttons ffmpeg actually opened during detection.

        Until detection returns, hardware is unknown rather than unavailable: saying
        "ffmpeg could not open it" at startup would be a lie the user has no way to
        disprove, and detection can genuinely take tens of seconds on a hung GPU stack.
        """
        for kind in (EncoderKind.NVENC, EncoderKind.QSV):
            button = self.encoder_buttons[kind]
            available = kind.value in self._encoders
            button.setEnabled(available)
            if available:
                button.setToolTip("")
            elif self._encoders_detected:
                button.setToolTip(
                    tr(
                        "{codec}: ffmpeg could not open this codec on this machine.",
                        codec=kind.name,
                    )
                )
            else:
                button.setToolTip(tr("Checking whether ffmpeg can open this codec…"))

    def _apply_encoder_selection(self, *, manage_signals: bool = True) -> None:
        """Resolve the wanted codec into ``state.encoder`` and check its radio.

        ``_wanted_encoder`` is what the user asked for (settings, project file, or a
        click); the state may only carry it if ffmpeg actually opened that codec.
        """
        wanted = self._wanted_encoder if self._wanted_encoder is not None else self.state.encoder
        # Falling back to CPU rather than AUTO keeps the choice honest: someone who
        # asked for a specific hardware codec gets a visible software encode, not an
        # "automatic" mode that could silently pick a different codec tomorrow.
        chosen = wanted if self.encoder_buttons[wanted].isEnabled() else EncoderKind.CPU
        buttons = list(self.encoder_buttons.values())
        if manage_signals:
            for widget in buttons:
                widget.blockSignals(True)
        self.state.set_encoder(chosen)
        self.encoder_buttons[chosen].setChecked(True)
        if manage_signals:
            for widget in buttons:
                widget.blockSignals(False)

    def _start_encoder_probe(self) -> None:
        probe = EncoderProbeWorker()
        self._encoder_probe = probe
        probe.succeeded.connect(self._on_encoder_probe_ok)
        probe.failed.connect(self._on_encoder_probe_fail)
        probe.finished.connect(probe.deleteLater)
        probe.start()

    def _on_encoder_probe_ok(self, detected: object) -> None:
        self._encoder_probe = None
        if self._closing or not isinstance(detected, set):
            return
        self._encoders = {str(name) for name in detected}
        self._encoders_detected = True
        self._apply_encoder_availability()
        # Detection may resolve AUTO into a hardware codec, which rewrites
        # ``state.encoder``. That is the program settling on a choice, not the user
        # editing anything, so a window that was clean must not grow an unsaved star.
        was_clean = not self._is_dirty()
        self._apply_encoder_selection()
        if was_clean:
            self._mark_saved()
        self._sync_ui()

    def _on_encoder_probe_fail(self, message: str) -> None:
        """Detection itself broke: stay on CPU and stop saying "checking" forever."""
        LOGGER.warning("Encoder detection failed: %s", message)
        self._encoder_probe = None
        if self._closing:
            return
        self._encoders = {EncoderKind.CPU.value}
        self._encoders_detected = True
        self._apply_encoder_availability()
        for kind in (EncoderKind.NVENC, EncoderKind.QSV):
            self.encoder_buttons[kind].setToolTip(tr("Could not check the hardware codec."))
        self._apply_encoder_selection()
        self._sync_ui()

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
        self.state.set_audio_slot(int(data) if data is not None else None)
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
                # Hardware buttons are still disabled here: detection runs in the
                # background, so remember the wish and honour it when it returns.
                self._wanted_encoder = encoder
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
        # A loaded project carries its own codec wish; keep it as the wanted one so a
        # later detection result can honour it instead of silently staying on AUTO.
        self._wanted_encoder = self.state.encoder
        self._apply_encoder_selection()
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
        self._refresh_title()

    def _is_dirty(self) -> bool:
        """Whether the screen holds edits the project file does not.

        Comparing against the signature taken at load/save time (instead of a flag set
        by every widget handler) means nothing counts as a change unless it would really
        be lost: scrubbing the playhead or letting detection pick a codec leaves the
        signature untouched, while dropping a clip or moving a mark does not.
        """
        directory = self._project_path.parent if self._project_path is not None else Path.home()
        return state_signature(self.state, directory) != self._saved_signature

    def _mark_saved(self, path: Path | None = None) -> None:
        """Remember the file the screen now matches, so the star and the prompt agree."""
        if path is not None:
            self._project_path = path
        directory = self._project_path.parent if self._project_path is not None else Path.home()
        self._saved_signature = state_signature(self.state, directory)
        self._refresh_title()

    def _refresh_title(self) -> None:
        name = self._project_path.name if self._project_path is not None else tr("new project")
        star = " *" if self._is_dirty() else ""
        self.setWindowTitle(tr("Video Puzzle — {name}{star}", name=name, star=star))

    def _confirm_discarding(self) -> bool:
        """Ask before throwing away work. Returns False to keep the current project."""
        if not self._is_dirty():
            return True
        answer = QMessageBox.question(
            self,
            tr("Unsaved changes"),
            tr("The current project has unsaved changes.\nThey will be lost. Continue?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _save_project(self, *, notify: bool = True) -> bool:
        last = self._settings.value("lastProjectDir") or str(Path.home())
        suggested = str(Path(str(last)) / "puzzle.vproj")
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            tr("Save project"),
            suggested,
            tr("Video Puzzle project (*.vproj *.json);;All files (*)"),
        )
        if not chosen:
            return False
        path = Path(chosen)
        if path.suffix.lower() not in {".vproj", ".json"}:
            path = path.with_suffix(".vproj")
        try:
            save_project(self.state, path)
        except ProjectError as exc:
            LOGGER.warning("Could not save project %s: %s", path, exc.detail or exc)
            QMessageBox.warning(self, tr("Project"), str(exc))
            return False
        self._settings.setValue("lastProjectDir", str(path.parent))
        self._mark_saved(path)
        if notify:
            QMessageBox.information(self, tr("Project"), tr("Saved: {path}", path=path))
        return True

    def _confirm_close(self) -> bool:
        """Save/Discard/Cancel prompt. Returns True when the window may close."""
        answer = QMessageBox.question(
            self,
            tr("Unsaved changes"),
            tr("The current project has unsaved changes.\nSave them before closing?"),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self._save_project(notify=False)
        return answer == QMessageBox.StandardButton.Discard

    def _open_project(self) -> None:
        last = (
            self._settings.value("lastProjectDir")
            or self._settings.value("lastOutputDir")
            or str(Path.home())
        )
        if not self._confirm_discarding():
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            tr("Open project"),
            str(last),
            tr("Video Puzzle project (*.vproj *.json);;All files (*)"),
        )
        if not chosen:
            return
        path = Path(chosen)
        missing: list[Path] = []
        try:
            loaded = load_project(path, missing=missing)
        except ProjectError as exc:
            LOGGER.warning("Could not open project %s: %s", path, exc.detail or exc)
            QMessageBox.warning(self, tr("Project"), str(exc))
            return
        # Replace the state and retire every in-flight worker: results computed for
        # the previous project must never land in the freshly loaded one.
        self.state = loaded
        self._project_epoch += 1
        self._sync_signature = None
        self._settings.setValue("lastProjectDir", str(path.parent))
        self._apply_state_to_widgets()
        self._mark_saved(path)
        if missing:
            shown = "\n".join(str(item) for item in missing[:10])
            more = tr("\n…and {count} more", count=len(missing) - 10) if len(missing) > 10 else ""
            QMessageBox.warning(
                self,
                tr("Files not found"),
                tr(
                    "The project was opened, but some video files are missing; "
                    "these cells remain empty:\n{shown}{more}",
                    shown=shown,
                    more=more,
                ),
            )

    def _preview_mosaic(self) -> None:
        if self._still_worker is not None:
            return
        if self._ffmpeg is None:
            QMessageBox.warning(
                self,
                tr("ffmpeg"),
                tr("ffmpeg was not found on PATH. Install it and restart the application."),
            )
            return
        dest = self._cache / "mosaic-preview.jpg"
        try:
            args = build_still_args(self.state, dest, ffmpeg=self._ffmpeg)
        except IncompleteStateError as exc:
            QMessageBox.warning(self, tr("Mosaic preview"), str(exc))
            return
        self.preview_mosaic_btn.setEnabled(False)
        worker = StillWorker(self._project_epoch, args, dest)
        worker.succeeded.connect(self._on_still_ok)
        worker.failed.connect(self._on_still_fail)
        worker.finished.connect(self._on_still_finished)
        self._still_worker = worker
        self._track_worker(worker)
        worker.start()

    def _on_still_ok(self, epoch: int, image: str) -> None:
        if epoch != self._project_epoch:
            return
        pixmap = QPixmap(image)
        if pixmap.isNull():
            QMessageBox.warning(self, tr("Mosaic preview"), tr("Could not open the frame."))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("Mosaic preview"))
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
        close = QPushButton(tr("Close"))
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _on_still_fail(self, epoch: int, message: str) -> None:
        if epoch != self._project_epoch:
            return
        QMessageBox.warning(self, tr("Mosaic preview"), message)

    def _on_still_finished(self) -> None:
        self._still_worker = None
        self._sync_ui()

    def _on_file_picked(self, index: int, path: Path) -> None:
        self.state.set_slot(index, path)
        self._probe_slot(index, path)
        self._request_thumbnail(index, path, loading=True)
        self._sync_ui()

    def _on_files_dropped(self, index: int, paths: list[Path]) -> None:
        if not paths:
            return
        first, *rest = paths
        self.state.set_slot(index, first)
        self._probe_slot(index, first)
        self._request_thumbnail(index, first, loading=True)
        leftover = 0
        if rest:
            before = [slot.path for slot in self.state.slots]
            # The drop happened on `index`, so the extras belong to the cells after it.
            # Filling from 0 used to overwrite cells the user never dropped on.
            leftover = len(rest) - self.state.assign_paths(rest, start=index + 1)
            for i, (old, new) in enumerate(
                zip(before, (slot.path for slot in self.state.slots), strict=True)
            ):
                if old != new and new is not None:
                    self._probe_slot(i, new)
                    self._request_thumbnail(i, new, loading=True)
        self._sync_ui()
        if leftover:
            QMessageBox.information(
                self,
                tr("Some files did not fit"),
                trn(
                    "There is not enough room after the selected cell for one more "
                    "file.\nExtend the layout or drop the files onto the first cell.",
                    "There are not enough free cells after the selected one for {n} more "
                    "files.\nExtend the layout or drop the files onto the first cell.",
                    leftover,
                ),
            )

    def _on_slot_cleared(self, index: int) -> None:
        self._thumb_gen[index] += 1
        self.state.clear_slot(index)
        self.canvas.slots[index].set_empty()
        self._sync_ui()

    def _on_playhead(self, seconds: float) -> None:
        self.state.set_playhead(seconds)
        self._scrub_timer.start()

    def _probe_slot(self, index: int, path: Path) -> None:
        worker = ProbeWorker(self._project_epoch, index, path)
        worker.succeeded.connect(self._on_probe_ok)
        worker.failed.connect(self._on_probe_fail)
        self._track_worker(worker)
        worker.start()

    def _slot_matches(self, epoch: int, index: int, video: str) -> bool:
        """True if slot `index` still holds the exact file a worker was started for.

        Covers every way the state can move under a running thread: another project
        loaded (epoch), a file replaced or cleared (path), slots swapped (path), the
        scheme shrunk so the index fell out of the active range (bounds).
        """
        if epoch != self._project_epoch:
            return False
        if not 0 <= index < self.state.active_count:
            return False
        slot_path = self.state.slots[index].path
        return slot_path is not None and str(slot_path) == video

    def _on_probe_ok(
        self,
        epoch: int,
        index: int,
        video: str,
        duration: float,
        has_audio: bool,
        has_video: bool,
        fps: float = 0.0,
    ) -> None:
        if not self._slot_matches(epoch, index, video):
            return
        if not has_video:
            message = tr("The file has no video stream (audio or data only)")
            self.state.set_probe_error(index, message)
            self.canvas.slots[index].set_error(Path(video), message)
            self._sync_ui()
            return
        self.state.set_probe(index, duration, has_audio, fps if fps > 0 else None)
        self._sync_ui()
        path = self.state.slots[index].path
        if path is not None:
            self._request_thumbnail(index, path, loading=False)

    def _on_probe_fail(self, epoch: int, index: int, video: str, message: str) -> None:
        if not self._slot_matches(epoch, index, video):
            return
        self.state.set_probe_error(index, message)
        self.canvas.slots[index].set_error(Path(video), message)
        self._sync_ui()

    def _request_thumbnail(self, index: int, path: Path, *, loading: bool) -> None:
        self._thumb_gen[index] += 1
        generation = self._thumb_gen[index]
        if loading:
            self.canvas.slots[index].set_loading(path)
        dest = self._cache / f"slot-{index}-{generation}.jpg"
        at = self.state.file_time(index)
        worker = ThumbnailWorker(self._project_epoch, index, generation, path, dest, at)
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

    def _on_thumb_ok(self, epoch: int, index: int, generation: int, video: str, dest: str) -> None:
        if not self._slot_matches(epoch, index, video):
            return
        if generation != self._thumb_gen[index]:
            return
        pixmap = QPixmap(dest)
        if pixmap.isNull():
            self.canvas.slots[index].set_error(Path(video), tr("Could not open the preview frame"))
            return
        self.canvas.slots[index].set_thumbnail(Path(video), pixmap)

    def _on_thumb_fail(
        self, epoch: int, index: int, generation: int, video: str, message: str
    ) -> None:
        if not self._slot_matches(epoch, index, video):
            return
        if generation != self._thumb_gen[index]:
            return
        self.canvas.slots[index].set_error(Path(video), message)

    def _sync_audio(self) -> None:
        if self.state.missing_slot_indexes():
            QMessageBox.information(
                self,
                tr("Synchronization"),
                tr("Select all videos in the current layout first."),
            )
            return
        self.sync_btn.setEnabled(False)
        self.align_note.setText(tr("Estimating the audio offset…"))
        self._sync_signature = _slots_signature(self.state.active_slots())
        worker = SyncWorker(self._project_epoch, self.state)
        worker.succeeded.connect(self._on_sync_ok)
        worker.failed.connect(self._on_sync_fail)
        self._track_worker(worker)
        worker.start()

    def _on_sync_ok(self, result: object, epoch: int) -> None:
        self.sync_btn.setEnabled(True)
        assert isinstance(result, SyncResult)
        if epoch != self._project_epoch or self._sync_signature != _slots_signature(
            self.state.active_slots()
        ):
            self.align_note.setText(tr("The layout changed; synchronization was not applied."))
            return
        apply_sync(self.state, result)
        self.align_note.setText(result.summary)
        self._sync_ui()
        self._refresh_all_thumbnails()

    def _on_sync_fail(self, epoch: int, message: str) -> None:
        self.sync_btn.setEnabled(True)
        if epoch != self._project_epoch:
            return
        self.align_note.setText(message)
        QMessageBox.warning(self, tr("Synchronization"), message)

    def _sync_ui(self) -> None:
        # Every path that mutates the state ends here, so the title's unsaved marker
        # cannot go stale the way a hand-placed call after each edit would.
        self._refresh_title()
        missing = self.state.missing_slot_indexes()
        missing_frags = self.state.missing_fragment_indexes()
        running = self._proc is not None
        probed = not missing and all(
            slot.duration is not None and slot.probe_error is None and slot.has_video
            for slot in self.state.active_slots()
        )
        failed = [
            index
            for index, slot in enumerate(self.state.active_slots())
            if slot.path is not None and slot.probe_error is not None
        ]
        ready = self.state.is_ready_for_export() and not running
        self.save_btn.setEnabled(ready)
        self.render_btn.setEnabled(ready)
        self.sync_btn.setEnabled(self.state.is_complete() and probed and not running)
        self.auto_frag_btn.setEnabled(
            self.state.is_wall and self.state.is_complete() and probed and not running
        )
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
            self.status.setText(trn("Select {n} more file.", "Select {n} more files.", n))
            self.script_view.setPlainText("")
            return
        if failed:
            cells = ", ".join(str(index + 1) for index in failed)
            self.status.setText(
                tr(
                    "Could not read the file(s) in cells {cells}. "
                    "Replace them or check that they are video files.",
                    cells=cells,
                )
            )
            self.script_view.setPlainText("")
            return
        if not probed:
            self.status.setText(tr("Checking the selected files…"))
            self.script_view.setPlainText("")
            return
        if missing_frags:
            cells = ", ".join(str(i + 1) for i in missing_frags)
            self.status.setText(
                tr(
                    "Video wall: mark a fragment (I/O) in cells {cells}; "
                    "otherwise the render would use the full clips.",
                    cells=cells,
                )
            )
            self.script_view.setPlainText("")
            return
        out_w, out_h = mosaic_output_size(self.state)
        extra = ""
        spread = self.state.duration_spread()
        if spread is not None and spread >= 0.2:
            extra = tr(" · durations differ by {seconds:.1f} s", seconds=spread)
        fragment = ""
        if self.state.is_wall:
            wall_len = self.state.wall_output_duration()
            if wall_len is not None:
                fragment = tr(" · wall {seconds:.2f} s", seconds=wall_len)
        elif self.state.range_enabled:
            start = self.state.resolved_range_start()
            end = self.state.resolved_range_end()
            if end is not None:
                fragment = tr(" · fragment {start:.2f}–{end:.2f} s", start=start, end=end)
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
            size_note = tr(" · ~{size}", size=format_size(nbytes))
        if running:
            self.status.setText(tr("Rendering video… You can stop it."))
        else:
            ready_note = tr(
                "Ready · output {width}×{height} · {quality} · {fps:.2f} fps",
                width=out_w,
                height=out_h,
                quality=_quality_status(self.state.quality),
                fps=self.state.output_fps(),
            )
            self.status.setText(
                f"{ready_note}{self._audio_status_note()}{size_note}{extra}{fragment}"
            )
        self.script_view.setPlainText(
            render_shell_script(self.state, Path("mosaic.mp4"), available_encoders=self._encoders)
        )

    def _audio_status_note(self) -> str:
        """Which clip's audio the ffmpeg script will actually map.

        Naming the resolved input (not ``state.audio_slot``) is what keeps the UI
        honest: a pin outside the scheme or on a silent clip resolves to None and
        the script carries "-an", so the status must say there is no sound instead
        of implying a source was selected.
        """
        if not self.state.include_audio:
            return ""
        index = self.state.audio_input_index()
        if index is None:
            return tr(" · audio: no clips with sound")
        return tr(" · audio: slot {slot}", slot=index + 1)

    def _sync_audio_combo(self) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem(tr("Auto (first with sound)"), None)
        for index, slot in enumerate(self.state.active_slots()):
            if slot.path is None:
                continue
            label = tr("Slot {slot} — {name}", slot=index + 1, name=slot.path.name)
            if not slot.has_audio:
                label += tr(" (no sound)")
            self.audio_combo.addItem(label, index)
            if not slot.has_audio:
                # Picking a silent clip used to produce "-an" while the combo still
                # showed the choice as if sound had been selected.
                model = self.audio_combo.model()
                if isinstance(model, QStandardItemModel):
                    item = model.item(self.audio_combo.count() - 1)
                    if item is not None:
                        item.setEnabled(False)
                        item.setToolTip(tr("This file has no audio track"))
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
            tr("Where to save the rendered video"),
            suggested,
            tr("Video (*.mp4);;All files (*)"),
        )
        if not chosen:
            return None
        output = Path(chosen)
        if output.suffix.lower() != ".mp4":
            output = output.with_suffix(".mp4")
        self._settings.setValue("lastOutputDir", str(output.parent))
        return output

    def _output_conflict(self, output: Path) -> Path | None:
        for slot in self.state.active_slots():
            if slot.path is not None and _same_file(output, slot.path):
                return slot.path
        return None

    def _warn_output_conflict(self, output: Path) -> bool:
        source = self._output_conflict(output)
        if source is None:
            return False
        QMessageBox.warning(
            self,
            tr("Output matches the source"),
            tr(
                "Cannot save the result over the source file:\n{source}\n\n"
                "Choose another name or folder; the source must remain untouched.",
                source=source,
            ),
        )
        return True

    def _save_script(self) -> None:
        if not self._confirm_export():
            return
        output = self._pick_output()
        if output is None:
            return
        if self._warn_output_conflict(output):
            return
        script_path = output.with_suffix(".sh")
        if script_path.exists():
            answer = QMessageBox.question(
                self,
                tr("Script already exists"),
                tr("The file {path} already exists. Overwrite it?", path=script_path),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            text = render_shell_script(self.state, output, available_encoders=self._encoders)
        except IncompleteStateError as exc:
            QMessageBox.warning(self, tr("Not enough files"), str(exc))
            return
        try:
            # newline="\n": a CRLF line ending breaks the shebang, so a script saved on
            # Windows still has to run under Git Bash/WSL.
            script_path.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            LOGGER.warning("Could not write script %s: %s", script_path, exc)
            QMessageBox.warning(
                self,
                tr("Not saved"),
                tr("Could not write the script {path}.", path=script_path),
            )
            return
        if os.name == "posix":
            os.chmod(script_path, 0o755)
            hint = f"chmod +x {script_path} && {script_path}"
        else:
            hint = f"bash {script_path}"
        QMessageBox.information(
            self,
            tr("Script saved"),
            tr(
                "Script: {script}\nOutput file in the command: {output}\n\nRun: {hint}",
                script=script_path,
                output=output,
                hint=hint,
            ),
        )

    def _render(self) -> None:
        if not self._confirm_export():
            return
        if self._ffmpeg is None:
            QMessageBox.warning(
                self,
                tr("ffmpeg"),
                tr("ffmpeg was not found on PATH. Install it and restart the application."),
            )
            return
        output = self._pick_output()
        if output is None:
            return
        if self._warn_output_conflict(output):
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
                tr("Build"),
                tr(
                    "Output {width}×{height}, about {size}.\nContinue?",
                    width=out_w,
                    height=out_h,
                    size=format_size(nbytes),
                ),
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
                ffmpeg=self._ffmpeg,
            )
        except IncompleteStateError as exc:
            QMessageBox.warning(self, tr("Not enough files"), str(exc))
            return
        self._render_total = self.state.export_duration() or 0.0
        self._render_cancelled = False
        self._render_output = output
        self._render_preexisting = output.exists()
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
            QMessageBox.warning(self, tr("ffmpeg"), tr("Could not start ffmpeg."))
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
        text = bytes(self._proc.readAllStandardOutput().data()).decode("utf-8", errors="replace")
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
        text = bytes(self._proc.readAllStandardError().data()).decode("utf-8", errors="replace")
        self._render_log += text
        if len(self._render_log) > 32_000:
            self._render_log = self._render_log[-16_000:]

    def _on_ffmpeg_finished(self, code: int, _status) -> None:
        cancelled = self._render_cancelled
        output = self._render_output
        preexisting = self._render_preexisting
        log = self._render_log
        self._proc = None
        self._render_cancelled = False
        self._render_output = None
        self._render_preexisting = False
        self._render_log = ""
        self._progress_buf = ""
        self._sync_ui()
        if self._closing:
            return
        exists = output is not None and output.exists()
        outcome = render_outcome(cancelled=cancelled, code=code, output_exists=exists)
        if should_delete_partial_output(outcome=outcome, preexisting=preexisting):
            assert output is not None
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
        if outcome == "cancelled":
            QMessageBox.information(
                self, tr("Render stopped"), tr("Video rendering was interrupted.")
            )
            return
        if outcome == "success":
            box = QMessageBox(self)
            box.setWindowTitle(tr("Done"))
            box.setText(tr("Video rendered."))
            open_file = box.addButton(tr("Open file"), QMessageBox.ButtonRole.AcceptRole)
            open_folder = box.addButton(tr("Open folder"), QMessageBox.ButtonRole.ActionRole)
            box.addButton(tr("Close"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if output is not None and clicked is open_file:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))
            elif output is not None and clicked is open_folder:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(output.parent)))
            return
        err = log.strip()
        if not exists:
            message = tr("The file was not created (code {code}).", code=code)
        else:
            message = tr("ffmpeg exited with code {code}", code=code)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("ffmpeg"))
        box.setText(message)
        if err:
            box.setDetailedText(err[-4000:])
        box.exec()

    def _track_worker(self, worker: QThread) -> None:
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w) if w in self._workers else None
        )
        self._workers.append(worker)

    def _shutdown_workers(self) -> None:
        """Cancel every thread and join it before the window is destroyed."""
        workers: list[QThread] = []
        for worker in (self._encoder_probe, self._still_worker, *self._workers):
            if worker is not None and worker not in workers:
                workers.append(worker)
        for worker in workers:
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
        deadline = time.monotonic() + WORKER_SHUTDOWN_SECONDS
        for worker in workers:
            remaining = max(0.0, deadline - time.monotonic())
            finished = worker.wait(int(remaining * 1000)) if remaining > 0 else worker.isFinished()
            if not finished:
                # Last resort: a stuck ffmpeg/GPU stack must not abort the whole app.
                worker.terminate()
                worker.wait(1000)
        self._workers.clear()
        self._still_worker = None
        self._encoder_probe = None

    def closeEvent(self, event) -> None:
        # Only an on-screen window can close with unsaved work at stake. Programmatic
        # closes (tests, a future tray shortcut) must not block on a modal dialog.
        if not self._closing and self.isVisible() and self._is_dirty():
            if not self._confirm_close():
                event.ignore()
                return
        self._closing = True
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("splitter", self.splitter.saveState())
        self._settings.setValue("quality", str(self.state.quality))
        self._settings.setValue("encoder", str(self.state.encoder))
        self._settings.setValue("cellGap", self.state.cell_gap)
        if self._proc is not None:
            self._render_cancelled = True
            self._proc.kill()
            if not self._proc.waitForFinished(3000):
                self._proc.terminate()
                self._proc.kill()
                self._proc.waitForFinished(1000)
            self._proc = None
        self._shutdown_workers()
        super().closeEvent(event)


def _quality_status(quality: EncodeQuality) -> str:
    return tr(
        {
            EncodeQuality.DRAFT: "draft",
            EncodeQuality.STANDARD: "standard",
            EncodeQuality.HIGH: "high",
            EncodeQuality.ORIGINAL: "original",
        }[quality]
    )
