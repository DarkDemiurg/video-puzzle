from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from video_puzzle.thumbnails import ThumbnailError, extract_thumbnail, ffmpeg_binary


def _ok(_cmd, **_kwargs) -> CompletedProcess[str]:
    return CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail(stderr: str = "boom") -> CompletedProcess[str]:
    return CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_ffmpeg_binary_missing() -> None:
    with patch("video_puzzle.thumbnails.shutil.which", return_value=None):
        with pytest.raises(ThumbnailError, match="not on PATH"):
            ffmpeg_binary()


def test_extract_thumbnail_success(tmp_path: Path) -> None:
    dest = tmp_path / "thumb.jpg"

    def runner(cmd, **kwargs):
        dest.write_bytes(b"jpeg")
        return _ok(cmd, **kwargs)

    result = extract_thumbnail(Path("/tmp/a.mp4"), dest, ffmpeg="/usr/bin/ffmpeg", runner=runner)
    assert result == dest
    assert dest.read_bytes() == b"jpeg"


def test_extract_thumbnail_retries_near_start(tmp_path: Path) -> None:
    dest = tmp_path / "thumb.jpg"
    seeks: list[str] = []

    def runner(cmd, **kwargs):
        seeks.append(cmd[cmd.index("-ss") + 1])
        if cmd[cmd.index("-ss") + 1] != "0.0":
            return _fail("no frame")
        dest.write_bytes(b"jpeg")
        return _ok(cmd, **kwargs)

    extract_thumbnail(Path("/tmp/a.mp4"), dest, at_seconds=1.0, ffmpeg="ffmpeg", runner=runner)
    assert seeks[0] == "1.0"
    assert seeks[-1] == "0.0"


def test_extract_thumbnail_does_not_fall_back_to_zero_when_scrubbing(tmp_path: Path) -> None:
    dest = tmp_path / "thumb.jpg"
    seeks: list[str] = []

    def runner(cmd, **kwargs):
        seeks.append(cmd[cmd.index("-ss") + 1])
        dest.write_bytes(b"jpeg")
        return _ok(cmd, **kwargs)

    extract_thumbnail(Path("/tmp/a.mp4"), dest, at_seconds=12.0, ffmpeg="ffmpeg", runner=runner)
    assert seeks == ["12.0"]


def test_extract_thumbnail_raises_after_retries(tmp_path: Path) -> None:
    dest = tmp_path / "thumb.jpg"

    def runner(cmd, **kwargs):
        dest.write_bytes(b"partial")
        return _fail("still broken")

    with pytest.raises(ThumbnailError, match="still broken"):
        extract_thumbnail(Path("/tmp/a.mp4"), dest, ffmpeg="ffmpeg", runner=runner)
    assert not dest.exists()
