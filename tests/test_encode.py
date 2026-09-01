from subprocess import CompletedProcess

from video_puzzle.encode import (
    EncodeQuality,
    EncoderKind,
    estimate_output_bytes,
    format_size,
    video_encoder_args,
)
from video_puzzle.encoders import (
    detect_encoders,
    parse_encoder_list,
    probe_encoder,
    resolve_encoder,
)


def test_format_size_uses_russian_units() -> None:
    assert format_size(500) == "500 Б"
    assert format_size(2048) == "2 КБ"
    assert format_size(2 * 1024 * 1024) == "2 МБ"


def test_estimate_grows_with_duration_and_quality() -> None:
    common = {"width": 1920, "height": 1080, "fps": 30.0, "has_audio": True}
    draft = estimate_output_bytes(duration=10.0, quality=EncodeQuality.DRAFT, **common)
    high = estimate_output_bytes(duration=10.0, quality=EncodeQuality.HIGH, **common)
    longer = estimate_output_bytes(duration=20.0, quality=EncodeQuality.HIGH, **common)
    assert draft > 0
    assert high > draft
    assert longer > high


def test_nvenc_and_qsv_encoder_args() -> None:
    nvenc = video_encoder_args(EncoderKind.NVENC, EncodeQuality.STANDARD)
    assert nvenc[nvenc.index("-c:v") + 1] == "h264_nvenc"
    assert "-cq" in nvenc
    qsv = video_encoder_args(EncoderKind.QSV, EncodeQuality.HIGH)
    assert qsv[qsv.index("-c:v") + 1] == "h264_qsv"
    assert qsv[qsv.index("-global_quality") + 1] == "18"


def test_auto_encoder_prefers_nvenc_then_qsv() -> None:
    assert resolve_encoder(EncoderKind.AUTO, {"libx264"}) is EncoderKind.CPU
    assert resolve_encoder(EncoderKind.AUTO, {"h264_nvenc", "libx264"}) is EncoderKind.NVENC
    assert resolve_encoder(EncoderKind.AUTO, {"h264_qsv", "libx264"}) is EncoderKind.QSV
    assert resolve_encoder(EncoderKind.NVENC, {"libx264"}) is EncoderKind.CPU


def test_parse_encoder_list() -> None:
    listing = " V..... h264_nvenc           NVIDIA NVENC H.264\n V..... libx264\n"
    names = parse_encoder_list(listing)
    assert "h264_nvenc" in names
    assert "libx264" in names
    assert "h264_qsv" not in names


def test_listed_nvenc_is_ignored_when_cuda_is_missing() -> None:
    listing = " V..... h264_nvenc\n V..... libx264\n"

    def runner(cmd, **_kwargs):
        if "-encoders" in cmd:
            return CompletedProcess(args=cmd, returncode=0, stdout=listing, stderr="")
        return CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="Cannot load libcuda.so.1",
        )

    names = detect_encoders(ffmpeg="ffmpeg", runner=runner)
    assert "h264_nvenc" not in names
    assert "libx264" in names
    assert resolve_encoder(EncoderKind.AUTO, names) is EncoderKind.CPU


def test_probe_encoder_accepts_zero_exit() -> None:
    def runner(cmd, **_kwargs):
        assert "-c:v" in cmd
        assert "h264_nvenc" in cmd
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    assert probe_encoder("h264_nvenc", ffmpeg="ffmpeg", runner=runner) is True
