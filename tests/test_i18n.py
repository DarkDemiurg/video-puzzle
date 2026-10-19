from __future__ import annotations

import ast
import re
import string
from pathlib import Path

import pytest

from video_puzzle.i18n import (
    available_languages,
    detect_language,
    get_language,
    normalize_language,
    set_language,
    tr,
    trn,
)
from video_puzzle.translations.ru import CATALOG

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "video_puzzle"
CYRILLIC = re.compile("[А-Яа-яЁё]")


@pytest.fixture(autouse=True)
def _english_after_each_test():
    set_language("en")
    yield
    set_language("en")


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _source_files() -> list[Path]:
    return [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if "translations" not in path.parts and path.name != "i18n.py"
    ]


def test_normalize_language_accepts_common_locale_forms() -> None:
    assert normalize_language("ru_RU.UTF-8") == "ru"
    assert normalize_language("ru-RU") == "ru"
    assert normalize_language("Russian_Russia") == "ru"
    assert normalize_language("en_US") == "en"
    assert normalize_language("C") is None
    assert normalize_language(None) is None
    assert set_language("de") == "en"


def test_detect_language_prefers_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VIDEO_PUZZLE_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    assert detect_language("en_US") == "ru"
    monkeypatch.setenv("VIDEO_PUZZLE_LANG", "en")
    assert detect_language("ru_RU") == "en"


def test_available_languages_are_native_and_ordered() -> None:
    assert list(available_languages().items()) == [("en", "English"), ("ru", "Русский")]


def test_russian_translation_and_placeholders() -> None:
    set_language("ru")
    assert get_language() == "ru"
    assert tr("Files not found") == "Файлы не найдены"
    assert tr("Saved: {path}", path="clip.vproj") == "Сохранено: clip.vproj"
    assert tr("Unknown message") == "Unknown message"


def test_russian_plural_forms() -> None:
    set_language("ru")
    singular = "Select {n} more file."
    plural = "Select {n} more files."
    assert trn(singular, plural, 1) == "Нужно выбрать ещё 1 файл."
    assert trn(singular, plural, 2) == "Нужно выбрать ещё 2 файла."
    assert trn(singular, plural, 5) == "Нужно выбрать ещё 5 файлов."
    assert trn(singular, plural, 11) == "Нужно выбрать ещё 11 файлов."
    assert trn(singular, plural, 21) == "Нужно выбрать ещё 21 файл."
    assert trn(singular, plural, 22) == "Нужно выбрать ещё 22 файла."


def test_english_plural_fallback() -> None:
    assert trn("one file", "many files", 1) == "one file"
    assert trn("one file", "many files", 2) == "many files"


def test_source_strings_are_english() -> None:
    violations: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and CYRILLIC.search(node.value)
            ):
                relative = path.relative_to(SOURCE_ROOT.parents[1])
                violations.append(f"{relative}:{node.lineno}: {node.value!r}")
    assert not violations, "user-facing source strings must be English:\n" + "\n".join(violations)


def test_catalog_covers_every_literal_translation_call() -> None:
    missing: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "tr" and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    if argument.value not in CATALOG:
                        missing.append(f"{path.name}:{node.lineno}: {argument.value!r}")
            elif node.func.id == "trn" and len(node.args) >= 2:
                pair = node.args[:2]
                if all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str) for item in pair
                ):
                    key = (pair[0].value, pair[1].value)
                    if key not in CATALOG:
                        missing.append(f"{path.name}:{node.lineno}: {key!r}")
    assert not missing, "missing Russian translations:\n" + "\n".join(missing)


def test_dynamic_labels_are_in_catalog() -> None:
    from video_puzzle.ui.main_window import _ENCODER_LABELS, _QUALITY_LABELS, _QUALITY_TIPS

    for source in (*_QUALITY_LABELS.values(), *_QUALITY_TIPS.values(), *_ENCODER_LABELS.values()):
        assert source in CATALOG
    for source in ("draft", "standard", "high", "original"):
        assert source in CATALOG


def _fields(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def test_catalog_keeps_format_placeholders() -> None:
    for key, value in CATALOG.items():
        if isinstance(key, tuple):
            assert isinstance(value, tuple) and len(value) == 3
            assert _fields(key[0]) == _fields(value[0]), key
            for form in value[1:]:
                assert _fields(key[1]) == _fields(form), key
        else:
            assert isinstance(value, str)
            assert _fields(key) == _fields(value), key


def test_russian_domain_errors_are_localized(tmp_path: Path) -> None:
    from subprocess import CompletedProcess
    from unittest.mock import patch

    from video_puzzle.probe import ProbeError, ffprobe_binary, probe_video
    from video_puzzle.project import ProjectError, load_project, save_project
    from video_puzzle.state import AppState, Slot
    from video_puzzle.sync import SyncError
    from video_puzzle.sync import ffmpeg_binary as sync_ffmpeg_binary
    from video_puzzle.thumbnails import ThumbnailError, extract_thumbnail
    from video_puzzle.wall import AutoFragmentError, apply_shortest_from_start

    set_language("ru")

    with patch("video_puzzle.probe.shutil.which", return_value=None):
        with pytest.raises(ProbeError, match="ffprobe не установлен"):
            ffprobe_binary()
    with patch("video_puzzle.sync.shutil.which", return_value=None):
        with pytest.raises(SyncError, match="ffmpeg не установлен"):
            sync_ffmpeg_binary()

    def runner(cmd, **_kwargs):
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

    with pytest.raises(ProbeError, match="Не удалось прочитать метаданные видео a.mp4") as probe:
        probe_video(tmp_path / "a.mp4", ffprobe="ffprobe", runner=runner)
    assert probe.value.detail == "boom"

    def broken_runner(*_args, **_kwargs):
        raise OSError("no exec")

    with pytest.raises(ProbeError, match="Не удалось запустить ffprobe для a.mp4") as start:
        probe_video(tmp_path / "a.mp4", ffprobe="ffprobe", runner=broken_runner)
    assert start.value.detail == "no exec"

    with pytest.raises(ThumbnailError, match="Не удалось извлечь кадр из a.mp4") as thumb:
        extract_thumbnail(tmp_path / "a.mp4", tmp_path / "t.jpg", ffmpeg="ffmpeg", runner=runner)
    assert thumb.value.detail == "boom"

    project = tmp_path / "newer.vproj"
    project.write_text('{"version": 999}', encoding="utf-8")
    with pytest.raises(ProjectError, match="Проект создан более новой версией"):
        load_project(project)

    broken = tmp_path / "broken.vproj"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectError, match="Некорректный файл проекта") as invalid:
        load_project(broken)
    assert invalid.value.detail

    with patch("video_puzzle.project.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(ProjectError, match="Не удалось сохранить проект") as save:
            save_project(AppState(), tmp_path / "failed.vproj")
    assert save.value.detail == "disk full"
    assert list(tmp_path.glob(".*.tmp")) == []

    with pytest.raises(AutoFragmentError, match="Заполните все ячейки сетки"):
        apply_shortest_from_start([Slot()], 1)


def test_main_window_language_selector_retranslates_ui() -> None:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox

    from video_puzzle.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    settings = QSettings()
    previous = settings.value("language")
    settings.setValue("language", "en")
    set_language("en")
    window = MainWindow()
    try:
        window.language_combo.setCurrentIndex(window.language_combo.findData("ru"))
        assert get_language() == "ru"
        assert window.radio_puzzle.text() == "Пазл"
        assert window.range_check.text() == "Фрагмент выхода"
        assert "новый проект" in window.windowTitle()
        russian_box = QMessageBox()
        russian_box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        assert {"Да", "Нет"} <= {button.text().replace("&", "") for button in russian_box.buttons()}
        russian_box.deleteLater()

        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        assert get_language() == "en"
        assert window.radio_puzzle.text() == "Puzzle"
        assert window.range_check.text() == "Output range"
        assert "new project" in window.windowTitle()
        english_box = QMessageBox()
        english_box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        assert {"Yes", "No"} <= {button.text().replace("&", "") for button in english_box.buttons()}
        english_box.deleteLater()
    finally:
        window.close()
        if previous is None:
            settings.remove("language")
        else:
            settings.setValue("language", previous)
        set_language("en")
