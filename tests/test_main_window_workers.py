import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from video_puzzle.encode import EncoderKind  # noqa: E402
from video_puzzle.layout import AppMode, Layout  # noqa: E402
from video_puzzle.project import save_project  # noqa: E402
from video_puzzle.state import AppState  # noqa: E402
from video_puzzle.sync import ClipAlignment, SyncResult  # noqa: E402
from video_puzzle.ui import main_window as main_window_module  # noqa: E402
from video_puzzle.ui.main_window import (  # noqa: E402
    EncoderProbeWorker,
    MainWindow,
    StillWorker,
    ThumbnailWorker,
    _slots_signature,
)


class _RecordingMessageBox:
    """Модальные диалоги в тестах не показываются: всё пишется в ``messages``.

    ``answers`` — очередь ответов для модальных вопросов (``ask``): по умолчанию
    «отмена», чтобы тест случайно не подтвердил удаление или перезапись.
    """

    messages: list[tuple[str, str, str]] = []
    details: list[str] = []
    answers: list[int] = []
    Icon = QMessageBox.Icon
    StandardButton = QMessageBox.StandardButton
    ButtonRole = QMessageBox.ButtonRole

    def __init__(self, *_args) -> None:
        self.title = ""
        self.text_shown = ""
        self.buttons: dict[str, object] = {}

    def setWindowTitle(self, title: str) -> None:
        self.title = title

    def setText(self, text: str) -> None:
        self.text_shown = text

    def setIcon(self, _icon) -> None:
        return None

    def setDetailedText(self, text: str) -> None:
        type(self).details.append(text)

    def addButton(self, text: str, _role=None) -> object:
        self.buttons[text] = object()
        return self.buttons[text]

    def clickedButton(self) -> object:
        return None

    def exec(self) -> int:
        self.messages.append(("dialog", self.title, self.text_shown))
        return 0

    @classmethod
    def _answer(cls) -> int:
        return cls.answers.pop(0) if cls.answers else int(cls.StandardButton.No)

    @classmethod
    def information(cls, _parent, title: str, text: str, *_args) -> int:
        cls.messages.append(("information", title, text))
        return int(cls.StandardButton.Ok)

    @classmethod
    def warning(cls, _parent, title: str, text: str, *_args) -> int:
        cls.messages.append(("warning", title, text))
        return int(cls.StandardButton.Ok)

    @classmethod
    def question(cls, _parent, title: str, text: str, *_args) -> int:
        cls.messages.append(("question", title, text))
        return cls._answer()


def _window() -> MainWindow:
    assert QApplication.instance() is not None
    return MainWindow()


@pytest.fixture()
def make_window(monkeypatch):
    """Окно без реального потока детекции: результат подаёт сам тест."""

    def build() -> MainWindow:
        monkeypatch.setattr(MainWindow, "_start_encoder_probe", lambda self: None)
        return _window()

    return build


def test_window_opens_before_encoder_detection_finishes(make_window) -> None:
    window = make_window()
    # Detection is what used to block startup for tens of seconds; with nothing known
    # yet, hardware must be off and must not claim ffmpeg failed to open it.
    assert window._encoders == {"libx264"}
    assert window._encoders_detected is False
    for kind in (EncoderKind.NVENC, EncoderKind.QSV):
        button = window.encoder_buttons[kind]
        assert not button.isEnabled()
        assert "Checking" in button.toolTip()
    # "Авто" is safe before detection: it resolves to whatever is known to work.
    assert window.encoder_buttons[EncoderKind.AUTO].isEnabled()
    assert window.encoder_buttons[EncoderKind.AUTO].isChecked()
    window.close()


def test_saved_hardware_choice_waits_for_detection(make_window) -> None:
    window = make_window()
    # QSettings remembered NVENC from a session on a machine that had it.
    window._wanted_encoder = EncoderKind.NVENC
    window._apply_encoder_selection()
    assert window.state.encoder is EncoderKind.CPU

    window._on_encoder_probe_ok({"libx264", "h264_nvenc"})

    assert window._encoders_detected is True
    assert window.encoder_buttons[EncoderKind.NVENC].isEnabled()
    assert window.state.encoder is EncoderKind.NVENC
    assert window.encoder_buttons[EncoderKind.NVENC].isChecked()
    window.close()


def test_detection_without_hardware_says_so_honestly(make_window) -> None:
    window = make_window()
    window._wanted_encoder = EncoderKind.NVENC
    window._on_encoder_probe_ok({"libx264"})

    for kind in (EncoderKind.NVENC, EncoderKind.QSV):
        button = window.encoder_buttons[kind]
        assert not button.isEnabled()
        assert "could not open" in button.toolTip()
    assert window.state.encoder is EncoderKind.CPU
    window.close()


def test_detection_refreshes_the_script_preview(make_window, tmp_path: Path) -> None:
    # The script pane is rendered with whatever is known at the time; a late detection
    # result must rewrite it, otherwise the copyable command names a dead codec.
    window = make_window()
    _filled(window, tmp_path)
    for index in range(window.state.active_count):
        window.state.set_probe(index, 10.0, False)
    window._sync_ui()
    window._wanted_encoder = EncoderKind.QSV
    window._on_encoder_probe_ok({"libx264", "h264_qsv"})

    script = window.script_view.toPlainText()
    assert "h264_qsv" in script
    assert "libx264" not in script.splitlines()[-1]
    window.close()


@pytest.fixture()
def message_box(monkeypatch) -> type[_RecordingMessageBox]:
    _RecordingMessageBox.messages = []
    _RecordingMessageBox.details = []
    _RecordingMessageBox.answers = []
    monkeypatch.setattr(main_window_module, "QMessageBox", _RecordingMessageBox)
    return _RecordingMessageBox


def _filled(window: MainWindow, tmp_path: Path, layout: Layout = Layout.TWO_VERTICAL) -> None:
    state = AppState(layout=layout)
    for index in range(state.active_count):
        clip = tmp_path / f"clip{index}.mp4"
        clip.write_bytes(b"")
        state.set_slot(index, clip)
    window.state = state
    window._apply_state_to_widgets()


def _wall(window: MainWindow, tmp_path: Path, *, marks: tuple[int, ...] = ()) -> AppState:
    """Стена 2×2: файлы есть, метки фрагментов — только в указанных ячейках."""

    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 2)
    for index in range(4):
        clip = tmp_path / f"wall{index}.mp4"
        clip.write_bytes(b"")
        state.set_slot(index, clip)
        state.set_probe(index, 60.0, False)
    for index in marks:
        state.slots[index].set_marks(1.0, 5.0)
    window.state = state
    window._apply_state_to_widgets()
    return state


def test_wall_without_fragments_is_not_export_ready(make_window, tmp_path: Path) -> None:
    """Готовность стены раньше считалась по наличию файлов — ffmpeg брал ролики целиком."""

    window = make_window()
    _wall(window, tmp_path)

    assert window.state.is_complete()
    assert not window.state.is_ready_for_export()
    assert not window.render_btn.isEnabled()
    assert not window.save_btn.isEnabled()
    assert not window.preview_mosaic_btn.isEnabled()
    assert window.sync_btn.isEnabled()
    assert "fragment" in window.status.text()
    assert window.script_view.toPlainText() == ""
    window.close()


def test_wall_with_fragments_in_every_cell_is_ready(make_window, tmp_path: Path) -> None:
    window = make_window()
    _wall(window, tmp_path, marks=(0, 1, 2, 3))

    assert window.state.is_ready_for_export()
    assert window.render_btn.isEnabled()
    assert window.save_btn.isEnabled()
    assert "libx264" in window.script_view.toPlainText()
    window.close()


def test_probe_result_from_previous_project_is_dropped(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    clip = window.state.slots[0].path
    assert clip is not None
    stale_epoch = window._project_epoch
    # Emulate opening another project: state object replaced, epoch bumped.
    window._project_epoch += 1
    window.state = AppState(layout=Layout.FOUR_SQUARE)
    window.state.set_slot(0, clip)

    window._on_probe_ok(stale_epoch, 0, str(clip), 12.0, True, 30.0)

    assert window.state.slots[0].duration is None


def test_probe_result_for_replaced_file_is_dropped(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    old_clip = window.state.slots[0].path
    assert old_clip is not None
    new_clip = tmp_path / "other.mp4"
    new_clip.write_bytes(b"")
    window.state.set_slot(0, new_clip)

    window._on_probe_ok(window._project_epoch, 0, str(old_clip), 12.0, True, 30.0)

    assert window.state.slots[0].duration is None


def test_probe_result_outside_active_scheme_is_dropped(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path, layout=Layout.FOUR_SQUARE)
    extra = window.state.slots[3].path
    assert extra is not None
    window.state.set_layout(Layout.TWO_VERTICAL)

    window._on_probe_ok(window._project_epoch, 3, str(extra), 12.0, True, 30.0)

    assert window.state.slots[3].duration is None


def test_current_probe_result_is_applied(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    clip = window.state.slots[0].path
    assert clip is not None

    window._on_probe_ok(window._project_epoch, 0, str(clip), 12.0, True, 30.0)

    assert window.state.slots[0].duration == 12.0
    assert window.state.slots[0].has_audio is True
    window.close()


def test_sync_result_is_dropped_when_scheme_changed(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    result = SyncResult(
        trims=[0.5, 0.0],
        alignments=[
            ClipAlignment(index=0, trim_start=0.5, missing_tail=0.0, lag=0.0, score=1.0),
            ClipAlignment(index=1, trim_start=0.0, missing_tail=0.0, lag=-0.5, score=0.9),
        ],
        overlap=3.0,
        used_audio=True,
        summary="Synchronized by audio.",
    )
    window._sync_signature = _slots_signature(window.state.active_slots())
    # Scheme shrank to two slots from another project load — trims no longer match.
    window._project_epoch += 1
    window.state = AppState(layout=Layout.TWO_HORIZONTAL)
    for index in range(2):
        clip = tmp_path / f"other{index}.mp4"
        clip.write_bytes(b"")
        window.state.set_slot(index, clip)

    window._on_sync_ok(result, window._project_epoch - 1)

    assert window.state.slots[0].trim_start == 0.0
    assert "not applied" in window.align_note.text()


def test_sync_result_is_dropped_when_files_changed(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    result = SyncResult(
        trims=[0.5, 0.0],
        alignments=[
            ClipAlignment(index=0, trim_start=0.5, missing_tail=0.0, lag=0.0, score=1.0),
            ClipAlignment(index=1, trim_start=0.0, missing_tail=0.0, lag=-0.5, score=0.9),
        ],
        overlap=3.0,
        used_audio=True,
        summary="Synchronized by audio.",
    )
    window._sync_signature = _slots_signature(window.state.active_slots())
    swapped = [slot.path for slot in window.state.slots]
    window.state.swap_slots(0, 1)

    window._on_sync_ok(result, window._project_epoch)

    assert window.state.slots[0].trim_start == 0.0
    assert window.state.slots[1].path == swapped[0]
    assert "not applied" in window.align_note.text()


def test_matching_sync_result_is_applied(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    result = SyncResult(
        trims=[0.5, 0.0],
        alignments=[
            ClipAlignment(index=0, trim_start=0.5, missing_tail=0.0, lag=0.0, score=1.0),
            ClipAlignment(index=1, trim_start=0.0, missing_tail=0.0, lag=-0.5, score=0.9),
        ],
        overlap=3.0,
        used_audio=True,
        summary="Synchronized by audio.",
    )
    window._sync_signature = _slots_signature(window.state.active_slots())

    window._on_sync_ok(result, window._project_epoch)

    assert window.state.slots[0].trim_start == 0.5
    assert window.align_note.text() == "Synchronized by audio."
    window.close()


def test_thumbnail_from_previous_project_is_dropped(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    index = 0
    clip = window.state.slots[index].path
    assert clip is not None
    stale_epoch = window._project_epoch
    window._project_epoch += 1
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg")

    window._on_thumb_ok(stale_epoch, index, window._thumb_gen[index], str(clip), str(image))

    assert (
        "Extracting frame" in window.canvas.slots[index].preview.text()
        or window.canvas.slots[index].preview.pixmap().isNull()
    )
    window.close()


def test_current_thumbnail_is_shown(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    index = 0
    clip = window.state.slots[index].path
    assert clip is not None
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"")

    window._on_thumb_ok(
        window._project_epoch, index, window._thumb_gen[index], str(clip), str(image)
    )

    assert window.canvas.slots[index].filename.text() == clip.name
    window.close()


def test_audio_status_note_names_the_source_or_admits_silence(tmp_path: Path) -> None:
    window = _window()
    _filled(window, tmp_path)
    window.state.include_audio = True

    # Nothing probed yet: the app cannot promise sound.
    assert window._audio_status_note() == " · audio: no clips with sound"

    window.state.set_probe(0, 6.0, True)
    assert window._audio_status_note() == " · audio: slot 1"

    window.state.set_audio_slot(0)
    assert window._audio_status_note() == " · audio: slot 1"

    # Probed silence must be reported instead of a confident "slot 2".
    window.state.set_probe(1, 6.0, False)
    window.state.set_audio_slot(1)
    assert window._audio_status_note() == " · audio: no clips with sound"

    window.state.include_audio = False
    assert window._audio_status_note() == ""
    window.close()


def test_render_outcome_success_keeps_file(tmp_path: Path, monkeypatch, message_box) -> None:
    window = _window()
    _filled(window, tmp_path)
    output = tmp_path / "result.mp4"
    output.write_bytes(b"video")

    window._render_output = output
    window._render_preexisting = False
    window._render_cancelled = False
    window._on_ffmpeg_finished(0, 0)

    assert output.exists()
    assert message_box.messages[-1][:2] == ("dialog", "Done")
    assert message_box.messages[-1][2] == "Video rendered."
    window.close()


def test_render_zero_exit_without_file_is_failure(tmp_path: Path, monkeypatch, message_box) -> None:
    # ffmpeg sometimes exits 0 and writes nothing (unreachable output dir, empty graph).
    window = _window()
    _filled(window, tmp_path)
    output = tmp_path / "result.mp4"

    window._render_output = output
    window._render_preexisting = False
    window._render_cancelled = False
    window._on_ffmpeg_finished(0, 0)

    assert not output.exists()
    assert message_box.messages[-1][0] == "dialog"
    assert "not created" in message_box.messages[-1][2]
    window.close()


def test_failed_render_cleans_our_partial_file(tmp_path: Path, message_box) -> None:
    window = _window()
    _filled(window, tmp_path)
    output = tmp_path / "result.mp4"
    output.write_bytes(b"half written")

    window._render_output = output
    window._render_preexisting = False
    window._render_cancelled = False
    window._render_log = "Encoder got a wall of death"
    window._on_ffmpeg_finished(1, 0)

    assert not output.exists()
    assert message_box.messages[-1][2] == "ffmpeg exited with code 1"
    assert "Encoder got a wall of death" in message_box.details[-1]
    window.close()


def test_failed_render_keeps_file_that_existed_before(tmp_path: Path, message_box) -> None:
    # ffmpeg was told to overwrite the user's own take and broke it halfway: deleting
    # it as "partial output" would destroy data we never produced.
    window = _window()
    _filled(window, tmp_path)
    output = tmp_path / "result.mp4"
    output.write_bytes(b"precious")

    window._render_output = output
    window._render_preexisting = True
    window._render_cancelled = False
    window._on_ffmpeg_finished(1, 0)

    assert output.read_bytes() == b"precious"
    window.close()


def test_cancelled_render_cleans_partial_but_not_previous_take(tmp_path: Path, message_box) -> None:
    window = _window()
    _filled(window, tmp_path)
    ours = tmp_path / "ours.mp4"
    ours.write_bytes(b"partial")

    window._render_output = ours
    window._render_preexisting = False
    window._render_cancelled = True
    window._on_ffmpeg_finished(143, 0)

    assert not ours.exists()
    assert message_box.messages[-1][:2] == ("information", "Render stopped")

    previous = tmp_path / "previous.mp4"
    previous.write_bytes(b"keep me")
    window._render_output = previous
    window._render_preexisting = True
    window._render_cancelled = True
    window._on_ffmpeg_finished(143, 0)

    assert previous.read_bytes() == b"keep me"
    window.close()


def test_cancel_racing_a_finished_render_reports_success(tmp_path: Path, message_box) -> None:
    window = _window()
    _filled(window, tmp_path)
    output = tmp_path / "result.mp4"
    output.write_bytes(b"video")

    window._render_output = output
    window._render_preexisting = False
    window._render_cancelled = True
    window._on_ffmpeg_finished(0, 0)

    assert output.exists()
    assert message_box.messages[-1][:2] == ("dialog", "Done")
    window.close()


def test_dropping_extra_files_does_not_touch_earlier_cells(
    make_window, tmp_path: Path, message_box, monkeypatch
) -> None:
    """Лишние файлы дропа раньше уходили в ячейки слева от целевой."""

    monkeypatch.setattr(MainWindow, "_probe_slot", lambda self, index, path: None)
    monkeypatch.setattr(MainWindow, "_request_thumbnail", lambda self, index, path, **_: None)
    window = make_window()
    _filled(window, tmp_path, layout=Layout.FOUR_SQUARE)
    window.state.set_slot(3, None)
    keep = [slot.path for slot in window.state.slots[:3]]
    first = tmp_path / "first.mp4"
    extra = tmp_path / "extra.mp4"
    first.write_bytes(b"")
    extra.write_bytes(b"")

    window._on_files_dropped(3, [first, extra])

    assert [slot.path for slot in window.state.slots[:3]] == keep
    assert window.state.slots[3].path == first
    assert "did not fit" in message_box.messages[-1][1]
    window.close()


def test_dropping_extras_fills_only_cells_after_the_target(
    make_window, tmp_path: Path, message_box, monkeypatch
) -> None:
    monkeypatch.setattr(MainWindow, "_probe_slot", lambda self, index, path: None)
    monkeypatch.setattr(MainWindow, "_request_thumbnail", lambda self, index, path, **_: None)
    window = make_window()
    _filled(window, tmp_path, layout=Layout.FOUR_SQUARE)
    window.state.set_slot(1, None)
    window.state.set_slot(2, None)
    kept = window.state.slots[3].path
    clips = [tmp_path / f"drop{i}.mp4" for i in range(3)]
    for clip in clips:
        clip.write_bytes(b"")

    window._on_files_dropped(0, clips)

    assert window.state.slots[0].path == clips[0]
    assert window.state.slots[1].path == clips[1]
    assert window.state.slots[2].path == clips[2]
    assert window.state.slots[3].path == kept
    assert message_box.messages == []
    window.close()


@pytest.fixture()
def file_dialog(monkeypatch, tmp_path: Path):
    """Файловый диалог вместо настоящего: тест сам решает, что «выбрал» пользователь."""

    class _Chooser:
        open_path: str | None = None
        save_path: str | None = None

        @staticmethod
        def getOpenFileName(_parent, _title, _start, _filter) -> tuple[str, str]:
            return (_Chooser.open_path or "", "")

        @staticmethod
        def getSaveFileName(_parent, _title, _start, _filter) -> tuple[str, str]:
            return (_Chooser.save_path or "", "")

    monkeypatch.setattr(main_window_module, "QFileDialog", _Chooser)
    return _Chooser


def _project(tmp_path: Path, name: str = "saved.vproj") -> Path:
    state = AppState(layout=Layout.TWO_VERTICAL)
    for index in range(state.active_count):
        clip = tmp_path / f"{name}-{index}.mp4"
        clip.write_bytes(b"")
        state.set_slot(index, clip)
    path = tmp_path / name
    save_project(state, path)
    return path


def test_opening_another_project_asks_when_there_are_unsaved_edits(
    make_window, tmp_path: Path, message_box, file_dialog
) -> None:
    window = make_window()
    _filled(window, tmp_path)  # правка поверх «чистого» только что открытого окна
    assert window._is_dirty()

    file_dialog.open_path = str(_project(tmp_path, "other.vproj"))
    message_box.answers = [int(message_box.StandardButton.No)]

    window._open_project()

    assert message_box.messages[-1][:2] == ("question", "Unsaved changes")
    # Отказ = ничего не потеряно: старый набор файлов остался на экране.
    assert window.state.slots[0].path == tmp_path / "clip0.mp4"
    assert window._project_path is None
    window.close()


def test_confirming_opens_project_and_clears_unsaved_mark(
    make_window, tmp_path: Path, message_box, file_dialog
) -> None:
    window = make_window()
    _filled(window, tmp_path)
    other = _project(tmp_path, "other.vproj")

    file_dialog.open_path = str(other)
    message_box.answers = [int(message_box.StandardButton.Yes)]

    window._open_project()

    assert window.state.slots[0].path == other.parent / "other.vproj-0.mp4"
    assert window._project_path == other
    assert not window._is_dirty()
    assert "other.vproj" in window.windowTitle()
    assert "*" not in window.windowTitle()
    window.close()


def test_clean_project_opens_without_a_prompt(
    make_window, tmp_path: Path, message_box, file_dialog
) -> None:
    window = make_window()
    other = _project(tmp_path, "other.vproj")
    file_dialog.open_path = str(other)

    window._open_project()

    assert message_box.messages == []
    assert window._project_path == other
    assert not window._is_dirty()
    window.close()


def test_project_error_dialog_is_russian_without_raw_parser_text(
    make_window, tmp_path: Path, message_box, file_dialog
) -> None:
    from PySide6.QtCore import QSettings

    from video_puzzle.i18n import set_language
    from video_puzzle.ui.qt_translations import apply_qt_translations

    settings = QSettings()
    previous = settings.value("language")
    settings.setValue("language", "ru")
    set_language("ru")
    apply_qt_translations()
    window = make_window()
    try:
        broken = tmp_path / "broken.vproj"
        broken.write_text("{not json", encoding="utf-8")
        file_dialog.open_path = str(broken)

        window._open_project()

        assert message_box.messages[-1][:2] == ("warning", "Проект")
        assert message_box.messages[-1][2] == "Некорректный файл проекта"
    finally:
        window.close()
        if previous is None:
            settings.remove("language")
        else:
            settings.setValue("language", previous)
        set_language("en")
        apply_qt_translations()


def test_saving_stops_marking_the_project_unsaved(
    make_window, tmp_path: Path, message_box, file_dialog
) -> None:
    window = make_window()
    _filled(window, tmp_path)
    assert window._is_dirty()

    target = tmp_path / "out.vproj"
    file_dialog.save_path = str(target)

    window._save_project()

    assert target.exists()
    assert window._project_path == target
    assert not window._is_dirty()
    assert "*" not in window.windowTitle()
    window.close()


def test_encoder_detection_does_not_look_like_a_user_edit(
    tmp_path: Path, message_box, file_dialog
) -> None:
    window = _window()
    _filled(window, tmp_path)
    window._mark_saved()
    assert not window._is_dirty()

    window._on_encoder_probe_ok({"libx264", "h264_nvenc"})

    # AUTO resolves to a hardware codec: state changed, the user did not touch it.
    assert not window._is_dirty()
    assert "*" not in window.windowTitle()
    window.close()


def test_playhead_scrub_is_not_an_unsaved_change(make_window, tmp_path: Path, message_box) -> None:
    window = make_window()
    _filled(window, tmp_path)
    window.state.set_probe(0, 6.0, True)
    window.state.set_probe(1, 6.0, True)
    window._mark_saved()

    window._on_playhead(3.0)

    assert window.state.playhead == 3.0
    assert not window._is_dirty()
    window.close()


def test_encoder_worker_honours_the_subprocess_run_contract(monkeypatch) -> None:
    """``Popen`` does not accept ``capture_output``; the old runner forwarded it."""
    calls: dict[str, str] = {}

    def fake_detect(*, runner):
        result = runner(
            [sys.executable, "-c", "print('ok')"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        calls["stdout"] = result.stdout.strip()
        return {"libx264"}

    monkeypatch.setattr(main_window_module, "detect_encoders", fake_detect)
    worker = EncoderProbeWorker()
    results: list[object] = []
    worker.succeeded.connect(results.append)
    worker.start()
    assert worker.wait(5000)
    QApplication.processEvents()
    assert calls["stdout"] == "ok"
    assert results == [{"libx264"}]


def test_thumbnail_pixmap_reaches_the_preview(make_window, tmp_path: Path, monkeypatch) -> None:
    """A QPixmap passed to ``set_thumbnail`` used to be stringified and became null."""
    monkeypatch.setattr(MainWindow, "_probe_slot", lambda self, index, path: None)
    monkeypatch.setattr(MainWindow, "_request_thumbnail", lambda self, index, path, **_: None)
    window = make_window()
    _filled(window, tmp_path)
    window.show()
    QApplication.processEvents()
    index = 0
    clip = window.state.slots[index].path
    assert clip is not None
    image = tmp_path / "frame.jpg"
    pixmap = QPixmap(64, 36)
    pixmap.fill(Qt.GlobalColor.red)
    assert pixmap.save(str(image))

    window._on_thumb_ok(
        window._project_epoch, index, window._thumb_gen[index], str(clip), str(image)
    )

    widget = window.canvas.slots[index]
    assert not widget._pixmap.isNull()
    assert not widget.preview.pixmap().isNull()
    assert widget.current_path == clip
    window.close()


def test_close_cancels_a_slow_thumbnail_worker(make_window, tmp_path: Path, monkeypatch) -> None:
    """A stuck ffmpeg must not abort the app with "QThread: Destroyed while running"."""
    started_process = {"seen": False}

    def slow_extract(_video, _dest, *, at_seconds, runner):
        started_process["seen"] = True
        runner(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    monkeypatch.setattr(main_window_module, "extract_thumbnail", slow_extract)
    window = make_window()
    worker = ThumbnailWorker(0, 0, 0, tmp_path / "a.mp4", tmp_path / "thumb.jpg", 0.0)
    window._track_worker(worker)
    worker.start()
    deadline = time.monotonic() + 2.0
    while not started_process["seen"] and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)

    before = time.monotonic()
    window.close()
    elapsed = time.monotonic() - before

    assert started_process["seen"]
    assert elapsed < 5.0
    assert worker.isFinished()


def test_auto_fragments_are_available_for_a_filled_unmarked_wall(
    make_window, tmp_path: Path
) -> None:
    window = make_window()
    state = _wall(window, tmp_path)

    assert not state.is_ready_for_export()
    assert window.auto_frag_btn.isEnabled()

    window._auto_wall_fragments()

    assert all(slot.has_fragment for slot in state.active_slots())
    assert state.is_ready_for_export()
    window.close()


@pytest.mark.real_close_prompt
def test_cancel_in_the_close_prompt_keeps_the_window(
    make_window, tmp_path: Path, message_box
) -> None:
    window = make_window()
    window.show()
    _filled(window, tmp_path)
    assert window._is_dirty()

    message_box.answers = [int(message_box.StandardButton.Cancel)]
    window.close()
    assert window.isVisible()
    assert message_box.messages[-1][:2] == ("question", "Unsaved changes")

    message_box.answers = [int(message_box.StandardButton.Discard)]
    window.close()
    assert not window.isVisible()


def test_render_refuses_to_overwrite_an_input_through_a_symlink(
    make_window, tmp_path: Path, message_box, file_dialog
) -> None:
    window = make_window()
    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"precious source")
    other = tmp_path / "other.mp4"
    other.write_bytes(b"other")
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    state.set_slot(0, clip)
    state.set_slot(1, other)
    state.set_probe(0, 10.0, False)
    state.set_probe(1, 10.0, False)
    window.state = state
    window._apply_state_to_widgets()
    window._mark_saved()
    window._ffmpeg = "ffmpeg"

    output = tmp_path / "out.mp4"
    try:
        output.symlink_to(clip)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create file symlinks")
    file_dialog.save_path = str(output)

    window._render()

    assert clip.read_bytes() == b"precious source"
    assert any("matches the source" in title for _kind, title, _text in message_box.messages)
    assert window._proc is None
    window.close()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_output_conflict_detects_links(tmp_path: Path, link_kind: str) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    output = tmp_path / "out.mp4"
    if link_kind == "symlink":
        try:
            output.symlink_to(clip)
        except (OSError, NotImplementedError):
            pytest.skip("this platform cannot create file symlinks")
    else:
        os.link(clip, output)
    assert main_window_module._same_file(output, clip)
    assert main_window_module._same_file(tmp_path / "." / "clip.mp4", clip)


def test_growing_the_wall_restores_cells_hidden_by_shrink(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    monkeypatch.setattr(MainWindow, "_probe_slot", lambda self, index, path: None)
    requested: list[int] = []

    def fake_thumbnail(self, index: int, path: Path, *, loading: bool) -> None:
        requested.append(index)
        self.canvas.slots[index].set_loading(path)

    monkeypatch.setattr(MainWindow, "_request_thumbnail", fake_thumbnail)
    state = AppState()
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(3, 3)
    for index in range(9):
        clip = tmp_path / f"cell{index}.mp4"
        clip.write_bytes(b"")
        state.set_slot(index, clip)
        state.set_probe(index, 30.0, False)
        state.slots[index].set_marks(1.0, 5.0)
    window.state = state
    window._apply_state_to_widgets()

    window.rows_spin.setValue(2)
    window.cols_spin.setValue(2)
    window._apply_state_to_widgets()
    assert window.canvas.slots[8].current_path is None

    requested.clear()
    window.rows_spin.setValue(3)
    window.cols_spin.setValue(3)

    assert requested == list(range(4, 9))
    for index in range(9):
        assert window.canvas.slots[index].current_path == state.slots[index].path
    assert state.is_ready_for_export()
    window.close()


def test_empty_drop_is_ignored(make_window) -> None:
    window = make_window()
    window._on_files_dropped(0, [])
    assert window.state.missing_slot_indexes() == list(range(window.state.active_count))
    window.close()


def test_trim_editor_waits_until_the_duration_is_known(
    make_window, tmp_path: Path, message_box, monkeypatch
) -> None:
    constructed: list[object] = []

    def fail_if_constructed(*args, **kwargs):
        constructed.append(object())
        raise AssertionError("TrimEditor must not open before ffprobe finishes")

    monkeypatch.setattr(main_window_module, "TrimEditor", fail_if_constructed)
    window = make_window()
    _filled(window, tmp_path)
    assert window.state.slots[0].duration is None

    window._open_trim_editor(0)

    assert constructed == []
    assert message_box.messages[-1][0] == "information"
    assert "Wait until" in message_box.messages[-1][2]
    window.close()


def test_audio_only_probe_blocks_export(make_window, tmp_path: Path) -> None:
    window = make_window()
    _filled(window, tmp_path)
    clip = window.state.slots[0].path
    assert clip is not None

    window._on_probe_ok(window._project_epoch, 0, str(clip), 10.0, True, False, 0.0)

    assert window.state.slots[0].probe_error is not None
    assert not window.state.is_ready_for_export()
    assert not window.render_btn.isEnabled()
    assert "No preview" in window.canvas.slots[0].preview.text()
    window.close()


def test_saving_a_script_asks_before_overwriting(
    make_window, tmp_path: Path, message_box, file_dialog
) -> None:
    window = make_window()
    _filled(window, tmp_path)
    for index in range(window.state.active_count):
        window.state.set_probe(index, 10.0, False)
    window._sync_ui()
    window._mark_saved()

    output = tmp_path / "out.mp4"
    script = output.with_suffix(".sh")
    script.write_text("old script", encoding="utf-8")
    file_dialog.save_path = str(output)

    message_box.answers = [int(message_box.StandardButton.No)]
    window._save_script()
    assert script.read_text(encoding="utf-8") == "old script"

    message_box.answers = [int(message_box.StandardButton.Yes)]
    window._save_script()
    assert "ffmpeg" in script.read_text(encoding="utf-8")
    window.close()


def test_still_worker_does_not_show_a_stale_preview(tmp_path: Path, message_box) -> None:
    """ffmpeg can return 0 without writing; an old JPEG must not pass as the new frame."""
    dest = tmp_path / "mosaic-preview.jpg"
    dest.write_bytes(b"stale frame")
    worker = StillWorker(0, [sys.executable, "-c", "import sys; sys.exit(1)"], dest)
    failures: list[str] = []
    worker.failed.connect(lambda _epoch, message: failures.append(message))
    worker.start()
    assert worker.wait(5000)
    QApplication.processEvents()

    assert failures
    assert not dest.exists()
