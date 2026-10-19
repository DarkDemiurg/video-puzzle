import os
import time
from pathlib import Path

import pytest

from video_puzzle.cache import (
    clear_cache,
    family_of,
    is_cache_file,
    prune_cache,
    prune_cache_dirs,
)


def _make(directory: Path, name: str, *, age: float = 0.0, now: float | None = None) -> Path:
    path = directory / name
    path.write_bytes(b"jpeg")
    stamp = (time.time() if now is None else now) - age
    os.utime(path, (stamp, stamp))
    return path


def test_only_own_files_are_recognized() -> None:
    assert is_cache_file("slot-3-17.jpg")
    assert is_cache_file("trim-4.jpg")
    assert not is_cache_file("mosaic-preview.jpg")
    assert not is_cache_file("slot-3-17.png")
    assert not is_cache_file("notes.txt")


def test_family_groups_rotations_per_slot() -> None:
    assert family_of("slot-3-17.jpg") == "slot-3"
    assert family_of("slot-10-2.jpg") == "slot-10"
    assert family_of("trim-4.jpg") == "trim"


def test_prune_keeps_newest_per_family(tmp_path: Path) -> None:
    now = time.time()
    newest = [_make(tmp_path, f"slot-0-{i}.jpg", age=i, now=now) for i in range(5)]
    oldest = _make(tmp_path, "slot-0-99.jpg", age=50, now=now)
    other_slot = _make(tmp_path, "slot-1-0.jpg", age=40, now=now)

    removed = prune_cache(tmp_path, keep=3, max_age_s=10_000, now=now)

    assert removed == 3
    assert oldest.exists() is False
    # Rotation is per slot: an old frame of a different slot survives the rotation.
    assert other_slot.exists()
    # newest is ordered oldest-last: the three most recent frames of slot 0 stay.
    for path in newest[:3]:
        assert path.exists()
    for path in newest[3:]:
        assert not path.exists()


def test_prune_drops_everything_past_max_age(tmp_path: Path) -> None:
    now = time.time()
    fresh = _make(tmp_path, "slot-0-1.jpg", age=10, now=now)
    stale = _make(tmp_path, "slot-0-2.jpg", age=3600, now=now)

    removed = prune_cache(tmp_path, keep=100, max_age_s=600, now=now)

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_prune_ignores_foreign_and_missing_files(tmp_path: Path) -> None:
    now = time.time()
    foreign = _make(tmp_path, "other-999.jpg", age=10, now=now)
    text = tmp_path / "readme.txt"
    text.write_text("keep me")

    assert prune_cache(tmp_path, keep=1, max_age_s=10_000, now=now) == 0
    assert foreign.exists()
    assert text.exists()


def test_prune_on_missing_directory_is_noop(tmp_path: Path) -> None:
    assert prune_cache(tmp_path / "absent") == 0
    assert clear_cache(tmp_path / "absent") == 0


def test_prune_rejects_nonsense_keep(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="keep"):
        prune_cache(tmp_path, keep=0)


def test_clear_cache_removes_only_frames(tmp_path: Path) -> None:
    _make(tmp_path, "slot-0-1.jpg")
    _make(tmp_path, "trim-2.jpg")
    keeper = tmp_path / "mosaic-preview.jpg"
    keeper.write_bytes(b"jpeg")

    assert clear_cache(tmp_path) == 2
    assert keeper.exists()


def test_prune_cache_dirs_keeps_current_and_fresh_instances(tmp_path: Path) -> None:
    now = time.time()
    current = tmp_path / "100-uuid"
    fresh = tmp_path / "200-uuid"
    stale = tmp_path / "300-uuid"
    for directory, age in ((current, 0), (fresh, 10), (stale, 24 * 3600 + 1)):
        directory.mkdir()
        (directory / "slot-0-1.jpg").write_bytes(b"jpeg")
        stamp = now - age
        os.utime(directory, (stamp, stamp))

    removed = prune_cache_dirs(tmp_path, keep=current, now=now)

    assert removed == 1
    assert current.exists()
    assert fresh.exists()
    assert not stale.exists()
