import subprocess
from subprocess import CompletedProcess
from unittest.mock import patch

from video_puzzle.encoders import detect_encoders, probe_encoder


def _proc(stdout: str = "", returncode: int = 0) -> CompletedProcess[str]:
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_detect_encoders_without_ffmpeg_falls_back_to_cpu() -> None:
    def runner(cmd, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("ffmpeg must not be started")

    with patch("video_puzzle.encoders.shutil.which", return_value=None):
        assert detect_encoders(runner=runner) == {"libx264"}


def test_detect_encoders_lists_only_software_encoder() -> None:
    def runner(cmd, **kwargs):
        return _proc(" V....D libx264                 libx264 H.264\n")

    assert detect_encoders(ffmpeg="ffmpeg", runner=runner) == {"libx264"}


def test_detect_encoders_confirms_nvenc_with_real_probe() -> None:
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if "-encoders" in cmd:
            return _proc(" V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n")
        return _proc(returncode=0)

    assert detect_encoders(ffmpeg="ffmpeg", runner=runner) == {"libx264", "h264_nvenc"}
    assert any("h264_nvenc" in cmd for cmd in calls[1:])


def test_detect_encoders_drops_encoder_that_fails_to_open() -> None:
    def runner(cmd, **kwargs):
        if "-encoders" in cmd:
            return _proc(" V....D h264_qsv             Intel QSV h264\n")
        return _proc("unsupported", returncode=1)

    assert detect_encoders(ffmpeg="ffmpeg", runner=runner) == {"libx264"}


def test_probe_encoder_passes_timeout() -> None:
    seen: dict[str, object] = {}

    def runner(cmd, **kwargs):
        seen.update(kwargs)
        return _proc()

    assert probe_encoder("h264_nvenc", ffmpeg="ffmpeg", runner=runner) is True
    assert seen["timeout"] == 8


def test_probe_encoder_timeout_is_not_available() -> None:
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

    assert probe_encoder("h264_nvenc", ffmpeg="ffmpeg", runner=runner) is False


def test_probe_encoder_missing_binary_is_not_available() -> None:
    def runner(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg")

    assert probe_encoder("h264_nvenc", ffmpeg="ffmpeg", runner=runner) is False


def test_detect_encoders_survives_list_timeout() -> None:
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

    assert detect_encoders(ffmpeg="ffmpeg", runner=runner) == {"libx264"}


def test_detect_encoders_survives_missing_binary() -> None:
    def runner(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg")

    assert detect_encoders(ffmpeg="ffmpeg", runner=runner) == {"libx264"}
