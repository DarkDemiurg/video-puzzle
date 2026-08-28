import sys

from PySide6.QtWidgets import QApplication

from video_puzzle.ui.main_window import MainWindow
from video_puzzle.ui.style import apply_theme


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Video Puzzle")
    app.setOrganizationName("video-puzzle")
    apply_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
