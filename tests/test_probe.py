import json
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from video_puzzle.probe import (
    PROBE_TIMEOUT_SECONDS,
    ProbeError,
    parse_frame_rate,
    parse_probe_json,
    probe_video,
)


def test_parse_probe_json_prefers_video_duration_over_format() -> None:
    payload = json.dumps(
        {
            "streams": [
                {"codec_type": "video", "duration": "9.5"},
                {"codec_type": "audio", "duration": "9.4"},
            ],
            "format": {"duration": "10.125"},
        }
    )
    result = parse_probe_json(payload)
    assert result.duration == pytest.approx(9.5)
    assert result.has_audio is True
    assert result.has_video is True


def test_parse_probe_json_without_audio() -> None:
    payload = json.dumps({"streams": [{"codec_type": "video", "duration": "3"}], "format": {}})
    result = parse_probe_json(payload)
    assert result.duration == pytest.approx(3.0)
    assert result.has_audio is False


def test_parse_probe_json_requires_duration() -> None:
    with pytest.raises(ProbeError):
        parse_probe_json(json.dumps({"streams": [], "format": {}}))


def test_parse_probe_json_reads_frame_rate() -> None:
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "duration": "4",
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30/1",
                }
            ],
            "format": {},
        }
    )
    result = parse_probe_json(payload)
    assert result.fps == pytest.approx(30000 / 1001)


def test_parse_frame_rate_rejects_junk() -> None:
    assert parse_frame_rate("0/0") is None
    assert parse_frame_rate("25") == pytest.approx(25.0)
    assert parse_frame_rate("999") is None


def test_probe_video_uses_runner(tmp_path: Path) -> None:
    video = tmp_path / "a.mp4"
    payload = json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "2.5"}})

    def runner(cmd, **kwargs):
        assert "-of" in cmd
        return CompletedProcess(args=cmd, returncode=0, stdout=payload, stderr="")

    result = probe_video(video, ffprobe="ffprobe", runner=runner)
    assert result.duration == pytest.approx(2.5)
    assert result.has_audio is False


def test_probe_video_passes_timeout(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def runner(cmd, **kwargs):
        seen.update(kwargs)
        payload = json.dumps({"streams": [], "format": {"duration": "1"}})
        return CompletedProcess(args=cmd, returncode=0, stdout=payload, stderr="")

    probe_video(tmp_path / "a.mp4", ffprobe="ffprobe", runner=runner)
    assert seen["timeout"] == PROBE_TIMEOUT_SECONDS


def test_probe_video_timeout_becomes_probe_error(tmp_path: Path) -> None:
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

    with pytest.raises(ProbeError, match="did not respond"):
        probe_video(tmp_path / "a.mp4", ffprobe="ffprobe", runner=runner)


def test_probe_video_missing_binary_becomes_probe_error(tmp_path: Path) -> None:
    def runner(cmd, **kwargs):
        raise FileNotFoundError("ffprobe")

    with pytest.raises(ProbeError, match="Could not start ffprobe"):
        probe_video(tmp_path / "a.mp4", ffprobe="ffprobe", runner=runner)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"streams": "x"}',
        '{"streams": [42]}',
        '{"streams": [], "format": []}',
    ],
)
def test_parse_probe_json_rejects_malformed_shapes(payload: str) -> None:
    with pytest.raises(ProbeError):
        parse_probe_json(payload)


def test_parse_probe_json_reports_audio_only_files() -> None:
    payload = json.dumps(
        {
            "streams": [{"codec_type": "audio", "duration": "12"}],
            "format": {"duration": "12"},
        }
    )
    result = parse_probe_json(payload)
    assert result.has_video is False
    assert result.has_audio is True
    assert result.duration == pytest.approx(12.0)


def test_video_duration_wins_over_a_longer_audio_track() -> None:
    """A 12 s audio track must not stretch a 10 s video (trim/progress would lie)."""

    payload = json.dumps(
        {
            "streams": [
                {"codec_type": "video", "duration": "10"},
                {"codec_type": "audio", "duration": "12"},
            ],
            "format": {"duration": "12"},
        }
    )
    result = parse_probe_json(payload)
    assert result.duration == pytest.approx(10.0)
