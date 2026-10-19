from video_puzzle.progress import (
    format_timecode,
    parse_progress_seconds,
    progress_fraction,
    render_outcome,
    should_delete_partial_output,
)


def test_parse_out_time_ms() -> None:
    # ffmpeg reports out_time_ms as microseconds (deprecated alias of out_time_us).
    assert parse_progress_seconds("out_time_ms=1500000") == 1.5


def test_parse_out_time_us() -> None:
    assert parse_progress_seconds("out_time_us=2000000") == 2.0


def test_parse_time_hms() -> None:
    assert parse_progress_seconds("frame=10 fps=30 time=00:01:02.50 bitrate=1") == 62.5


def test_parse_na_and_junk() -> None:
    assert parse_progress_seconds("out_time_ms=N/A") is None
    assert parse_progress_seconds("frame=1 fps=30") is None


def test_progress_fraction_clamps() -> None:
    assert progress_fraction(5, 10) == 0.5
    assert progress_fraction(20, 10) == 1.0
    assert progress_fraction(1, 0) == 0.0


def test_format_timecode() -> None:
    assert format_timecode(5.5) == "0:05.50"
    assert format_timecode(62.5) == "1:02.50"
    assert format_timecode(3723.0) == "1:02:03.00"


def test_format_timecode_carries_rounded_seconds_into_minutes() -> None:
    # 59.996 rounds to 60.00 s: it must roll over instead of printing "0:60.00".
    assert format_timecode(59.996) == "1:00.00"
    assert format_timecode(119.999) == "2:00.00"
    assert format_timecode(3599.996) == "1:00:00.00"
    assert format_timecode(-3.0) == "0:00.00"


def test_render_outcome() -> None:
    assert render_outcome(cancelled=False, code=0, output_exists=True) == "success"
    assert render_outcome(cancelled=True, code=0, output_exists=True) == "success"
    assert render_outcome(cancelled=True, code=9, output_exists=False) == "cancelled"
    assert render_outcome(cancelled=False, code=1, output_exists=False) == "failed"


def test_render_outcome_needs_a_written_file() -> None:
    # ffmpeg can exit 0 without producing anything (bad path, filtered out streams).
    assert render_outcome(cancelled=False, code=0, output_exists=False) == "failed"
    # A cancel racing a finished render still leaves a usable video.
    assert render_outcome(cancelled=True, code=0, output_exists=True) == "success"


def test_should_delete_partial_output() -> None:
    assert should_delete_partial_output(outcome="cancelled", preexisting=False) is True
    assert should_delete_partial_output(outcome="failed", preexisting=False) is True
    assert should_delete_partial_output(outcome="success", preexisting=False) is False
    # A file the user already had must survive, even if this run made a mess of it.
    assert should_delete_partial_output(outcome="cancelled", preexisting=True) is False
    assert should_delete_partial_output(outcome="failed", preexisting=True) is False
