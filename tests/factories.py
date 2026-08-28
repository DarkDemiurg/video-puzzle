from pathlib import Path

from video_puzzle.layout import Layout
from video_puzzle.state import AppState


def video(name: str) -> Path:
    return Path(f"/tmp/{name}.mp4")


def filled_state(
    layout: Layout = Layout.FOUR_SQUARE,
    resolution: int = 1080,
) -> AppState:
    state = AppState(layout=layout, resolution=resolution)
    for index in range(state.active_count):
        state.set_slot(index, video(f"clip{index}"))
    return state
