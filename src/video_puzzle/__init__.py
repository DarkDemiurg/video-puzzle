"""Desktop app that tiles videos into a mosaic via ffmpeg."""

__all__ = ["main"]


def main() -> None:
    from video_puzzle.app import main as _main

    _main()
