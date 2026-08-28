import time
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from video_puzzle.state import Slot
from video_puzzle.ui.trim_editor import TrimEditor

_APP = None


def _app() -> QApplication:
    global _APP
    instance = QApplication.instance()
    if instance is None:
        _APP = QApplication([])
        return _APP
    return instance


def test_rapid_frame_nudge_runs_one_thread_at_a_time(tmp_path: Path) -> None:
    app = _app()
    live = {"count": 0, "max": 0}

    def slow_extract(video: Path, dest: Path, *, at_seconds: float = 1.0, **_kwargs) -> Path:
        live["count"] += 1
        live["max"] = max(live["max"], live["count"])
        time.sleep(0.12)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        live["count"] -= 1
        return dest

    slot = Slot(path=Path("/tmp/clip.mp4"), duration=10.0, mark_in=1.0, mark_out=5.0)
    with patch("video_puzzle.ui.trim_editor.extract_thumbnail", slow_extract):
        editor = TrimEditor(slot, tmp_path)
        for _ in range(10):
            editor._nudge(1 / 30)
            app.processEvents()
        deadline = time.monotonic() + 3.0
        while editor._worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        editor.close()
        app.processEvents()
    assert live["max"] == 1
    assert editor._worker is None
