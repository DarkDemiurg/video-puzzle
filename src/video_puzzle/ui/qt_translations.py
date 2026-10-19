"""Qt's own translations for standard dialogs and buttons.

The application strings use :mod:`video_puzzle.i18n`, but Qt draws standard button
labels (Yes/No/Save/Cancel) and native dialog captions itself.  PySide6 ships those
translations as ``qtbase_<language>.qm``; loading one keeps message boxes fully in
the selected language without adding a build step or shipping our own ``.qm`` files.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtWidgets import QApplication

from video_puzzle.i18n import get_language

LOGGER = logging.getLogger(__name__)
_translator: QTranslator | None = None


def apply_qt_translations(app: QApplication | None = None) -> None:
    """Install Qt's catalog for the active language, or remove it for English."""
    global _translator
    application = app or QApplication.instance()
    if application is None:
        return
    if _translator is not None:
        application.removeTranslator(_translator)
        _translator.deleteLater()
        _translator = None
    language = get_language()
    if language == "en":
        return
    directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator()
    if translator.load(f"qtbase_{language}", directory):
        application.installTranslator(translator)
        _translator = translator
    else:
        LOGGER.warning("Qt translations for %s not found in %s", language, directory)
