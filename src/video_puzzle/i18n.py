"""Lightweight runtime translations.

English is the source language: every user-facing string in the code is written in
English and passed through :func:`tr` (or :func:`trn` for plurals).  A language
catalog is a plain Python mapping, so adding a language needs no external build tools
and works in PyApp bundles as well as in a regular wheel.

The active language is chosen at startup from (in order) ``VIDEO_PUZZLE_LANG``,
``LANGUAGE``/``LC_ALL``/``LC_MESSAGES``/``LANG``, the Qt system locale, or the
Python locale.  English is the fallback.  ``set_language`` may be called later, for
example from the language selector in the sidebar.
"""

from __future__ import annotations

import locale
import os

type MessageKey = str | tuple[str, str]
type Translation = str | tuple[str, str, str]

DEFAULT_LANGUAGE = "en"

# Language code -> name shown to the user.  Native names are deliberately not
# translated: someone who accidentally picked the wrong language can still find
# their own language in the selector.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "ru": "Русский",
}

_CATALOG_MODULES = {
    "ru": "video_puzzle.translations.ru",
}

_current_language = DEFAULT_LANGUAGE
_catalog: dict[MessageKey, Translation] = {}


def available_languages() -> dict[str, str]:
    """Supported languages in menu order."""
    return dict(LANGUAGES)


def normalize_language(code: str | None) -> str | None:
    """Turn ``ru_RU.UTF-8``, ``ru-RU`` or ``Russian_Russia`` into ``ru``."""
    if not code:
        return None
    text = code.strip().replace("-", "_")
    text = text.split(".", 1)[0].split("@", 1)[0]
    base = text.split("_", 1)[0].lower()
    aliases = {"russian": "ru", "english": "en"}
    base = aliases.get(base, base)
    return base if base in LANGUAGES else None


def detect_language(system_locale: str | None = None) -> str:
    """Best-effort language of the user's desktop, without importing Qt."""
    for variable in ("VIDEO_PUZZLE_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(variable)
        if not raw:
            continue
        # LANGUAGE may be a colon-separated preference list.
        for item in raw.split(":"):
            code = normalize_language(item)
            if code is not None:
                return code
    if system_locale:
        code = normalize_language(system_locale)
        if code is not None:
            return code
    try:
        code = normalize_language(locale.getlocale(locale.LC_MESSAGES)[0])
    except (AttributeError, ValueError):
        code = None
    return code or DEFAULT_LANGUAGE


def get_language() -> str:
    return _current_language


def set_language(code: str | None) -> str:
    """Activate a language and return the code that was actually selected."""
    global _catalog, _current_language
    selected = normalize_language(code) or DEFAULT_LANGUAGE
    _current_language = selected
    _catalog = _load_catalog(selected)
    return selected


def _load_catalog(language: str) -> dict[MessageKey, Translation]:
    module_name = _CATALOG_MODULES.get(language)
    if module_name is None:
        return {}
    from importlib import import_module

    module = import_module(module_name)
    return dict(module.CATALOG)


def tr(message: str, /, **values: object) -> str:
    """Translate an English message, then fill ``str.format`` placeholders."""
    translated = _catalog.get(message, message)
    if not isinstance(translated, str):
        # A plural entry used without a count: fall back to the English source.
        translated = message
    return translated.format(**values) if values else translated


def trn(singular: str, plural: str, n: int, /, **values: object) -> str:
    """Translate a message with a count-aware plural form.

    Catalog entries for plural messages are three-tuples (one/few/many for Russian).
    The English source strings are used as keys, so the catalog stays readable.
    """
    entry = _catalog.get((singular, plural))
    if isinstance(entry, tuple):
        text = entry[min(_plural_index(n, _current_language), len(entry) - 1)]
    else:
        text = singular if abs(n) == 1 else plural
    return text.format(n=n, **values)


def _plural_index(n: int, language: str) -> int:
    value = abs(n)
    if language == "ru":
        if value % 10 == 1 and value % 100 != 11:
            return 0
        if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
            return 1
        return 2
    return 0 if value == 1 else 1
