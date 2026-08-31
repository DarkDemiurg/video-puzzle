import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from video_puzzle.probe import ProbeError, parse_frame_rate, parse_probe_json, probe_video


def test_parse_probe_json_uses_format_duration_and_audio_flag() -> None:
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
    assert result.duration == pytest.approx(10.125)
    assert result.has_audio is True


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
