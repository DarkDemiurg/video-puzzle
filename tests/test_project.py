from pathlib import Path

import pytest

from tests.factories import filled_state, video
from video_puzzle.encode import EncodeQuality, EncoderKind
from video_puzzle.layout import AppMode, Layout
from video_puzzle.project import ProjectError, load_project, save_project
from video_puzzle.state import AppState


def test_project_roundtrip_keeps_layout_files_and_edits(tmp_path: Path) -> None:
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    clip_a.write_bytes(b"")
    clip_b.write_bytes(b"")
    state = AppState(layout=Layout.TWO_HORIZONTAL, resolution=720)
    state.set_quality(EncodeQuality.HIGH)
    state.set_encoder(EncoderKind.CPU)
    state.set_cell_gap(8)
    state.include_audio = True
    state.audio_slot = 1
    state.normalize_audio = True
    state.set_slot(0, clip_a)
    state.set_slot(1, clip_b)
    state.slots[0].trim_start = 1.5
    state.slots[0].rotation = 90
    state.slots[0].crop = (0.1, 0.2, 0.5, 0.6)
    dest = tmp_path / "job.vproj"
    save_project(state, dest)

    loaded = load_project(dest)
    assert loaded.layout is Layout.TWO_HORIZONTAL
    assert loaded.resolution == 720
    assert loaded.quality is EncodeQuality.HIGH
    assert loaded.encoder is EncoderKind.CPU
    assert loaded.cell_gap == 8
    assert loaded.audio_slot == 1
    assert loaded.normalize_audio is True
    assert loaded.slots[0].path == clip_a
    assert loaded.slots[1].path == clip_b
    assert loaded.slots[0].trim_start == pytest.approx(1.5)
    assert loaded.slots[0].rotation == 90
    assert loaded.slots[0].crop == pytest.approx((0.1, 0.2, 0.5, 0.6))


def test_project_stores_relative_paths(tmp_path: Path) -> None:
    clip = tmp_path / "inside.mp4"
    clip.write_bytes(b"")
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    state.set_slot(0, clip)
    state.set_slot(1, video("outside"))
    dest = tmp_path / "job.vproj"
    save_project(state, dest)
    text = dest.read_text(encoding="utf-8")
    assert '"path": "inside.mp4"' in text
    assert str(video("outside")) in text


def test_project_wall_and_range(tmp_path: Path) -> None:
    state = filled_state()
    for index in range(4):
        path = tmp_path / f"cell{index}.mp4"
        path.write_bytes(b"")
        state.set_slot(index, path)
        state.slots[index].set_marks(1.0, 4.0)
    state.set_mode(AppMode.WALL)
    state.set_wall_grid(2, 2)
    state.set_range_enabled(True)
    state.set_output_range(2.0, 8.0)
    dest = tmp_path / "wall.vproj"
    save_project(state, dest)
    loaded = load_project(dest)
    assert loaded.is_wall
    assert loaded.wall_rows == 2
    assert loaded.wall_cols == 2
    assert loaded.slots[0].mark_in == pytest.approx(1.0)
    assert loaded.slots[0].mark_out == pytest.approx(4.0)


def test_load_rejects_garbage(tmp_path: Path) -> None:
    dest = tmp_path / "bad.vproj"
    dest.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectError):
        load_project(dest)
