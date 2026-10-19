import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Tests assert the English source strings. Without this, the language chosen by
# MainWindow would follow the developer's desktop locale (for example ru_RU).
os.environ["VIDEO_PUZZLE_LANG"] = "en"

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _qt_test_app() -> object:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from video_puzzle.i18n import set_language

    set_language("en")
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("video-puzzle-tests")
    app.setApplicationName("video-puzzle-tests")
    # Окно читает и пишет настройки в QSettings. Без изоляции тест зависел бы от того,
    # какой кодек/схему оставил после себя настоящий запуск приложения на машине —
    # и падал бы только у того, у кого в профиле сохранился, скажем, NVENC.
    settings_dir = tempfile.mkdtemp(prefix="video-puzzle-settings-")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, settings_dir)
    return app


@pytest.fixture(autouse=True)
def _no_encoder_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оконные тесты не должны гонять реальный ``ffmpeg -encoders`` в потоке.

    Результат детекции тесты подают сами, вызывая ``_on_encoder_probe_ok``.
    """
    from video_puzzle.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_start_encoder_probe", lambda self: None)


@pytest.fixture(autouse=True)
def _no_media_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оконные тесты не запускают ffprobe/ffmpeg на пустых файлах.

    Результаты воркеров тесты подают сами через ``_on_probe_ok``/``_on_thumb_ok``.
    Иначе реальный ffprobe успевал «упасть» уже после того, как тест выставил
    корректные duration/has_audio, и состояние становилось неготовым.
    """
    from video_puzzle.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_probe_slot", lambda self, index, path: None)
    monkeypatch.setattr(MainWindow, "_request_thumbnail", lambda self, index, path, **_: None)


@pytest.fixture(autouse=True)
def _no_close_prompt(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Модальный Save/Discard/Cancel не должен блокировать обычные тесты.

    Тест, который проверяет сам диалог, помечается ``@pytest.mark.real_close_prompt``
    и получает настоящую реализацию.
    """
    if request.node.get_closest_marker("real_close_prompt") is not None:
        return
    from video_puzzle.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_confirm_close", lambda self: True)
