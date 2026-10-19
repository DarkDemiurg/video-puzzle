"""Housekeeping for the preview frame cache.

Every scrub of the timeline writes a fresh JPEG per slot (and the trim editor writes one per
frame request), so without pruning the cache directory grows by thousands of files per session.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# Only files we own are touched, so a shared XDG cache directory stays intact.
CACHE_PREFIXES = ("slot-", "trim-")

KEEP_PER_FAMILY = 6
MAX_AGE_SECONDS = 24 * 3600.0


def is_cache_file(name: str) -> bool:
    return name.startswith(CACHE_PREFIXES) and name.endswith(".jpg")


def family_of(name: str) -> str:
    """Group name for rotation: ``slot-3-17.jpg`` and ``trim-4.jpg`` keep their stem."""
    stem = name[: -len(".jpg")]
    head, _sep, tail = stem.rpartition("-")
    return head if head and tail.isdigit() else stem


def cache_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    return [Path(entry.path) for entry in entries if entry.is_file() and is_cache_file(entry.name)]


def prune_cache(
    directory: Path,
    *,
    keep: int = KEEP_PER_FAMILY,
    max_age_s: float = MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Drop frames older than ``max_age_s``, plus all but ``keep`` newest in each family."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    moment = time.time() if now is None else now
    families: dict[str, list[tuple[float, Path]]] = {}
    removed = 0
    for path in cache_files(directory):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if moment - modified > max_age_s:
            if _unlink(path):
                removed += 1
            continue
        families.setdefault(family_of(path.name), []).append((modified, path))
    for entries in families.values():
        entries.sort(key=lambda item: item[0], reverse=True)
        for _modified, path in entries[keep:]:
            if _unlink(path):
                removed += 1
    return removed


def clear_cache(directory: Path) -> int:
    """Remove every cached frame (used on startup, when nothing on disk is still referenced)."""
    return sum(1 for path in cache_files(directory) if _unlink(path))


def prune_cache_dirs(
    base: Path,
    *,
    max_age_s: float = MAX_AGE_SECONDS,
    keep: Path | None = None,
    now: float | None = None,
) -> int:
    """Remove per-process cache directories left behind by dead instances.

    Two running copies of the app must not delete each other's frames, so startup only
    clears its own directory. Directories untouched for ``max_age_s`` are stale (their
    process cannot still be scrubbing) and are removed to stop unbounded growth.
    """
    if not base.is_dir():
        return 0
    moment = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(base))
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path)
        if keep is not None and path.resolve() == keep.resolve():
            continue
        try:
            modified = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if moment - modified <= max_age_s:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed += 1
    return removed


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True
