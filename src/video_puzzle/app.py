import logging
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

from video_puzzle.ui.main_window import MainWindow
from video_puzzle.ui.style import apply_theme


def _log_file() -> Path:
    """Per-user log file; fall back to the system temp directory."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    directory = Path(base) if base else Path(tempfile.gettempdir()) / "video-puzzle"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "video-puzzle.log"


def _configure_logging() -> None:
    """Desktop runs have no console; keep tracebacks in a file instead of losing them."""
    try:
        handler = logging.FileHandler(_log_file(), encoding="utf-8")
    except OSError as exc:
        logging.basicConfig(level=logging.INFO)
        logging.warning("Could not open the log file: %s", exc)
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
    )

    def report(exc_type, exc, traceback) -> None:
        logging.critical("Unhandled exception", exc_info=(exc_type, exc, traceback))
        sys.__excepthook__(exc_type, exc, traceback)

    sys.excepthook = report

    def report_thread(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            logging.critical(
                "Unhandled exception in thread %s",
                args.thread.name if args.thread is not None else "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        threading.__excepthook__(args)

    threading.excepthook = report_thread


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Video Puzzle")
    app.setOrganizationName("video-puzzle")
    _configure_logging()
    logging.info("Starting Video Puzzle")
    apply_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
