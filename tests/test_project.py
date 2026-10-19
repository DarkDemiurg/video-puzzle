import json
from pathlib import Path

import pytest

from tests.factories import filled_state, video
from video_puzzle.encode import EncodeQuality, EncoderKind
from video_puzzle.layout import AppMode, Layout
from video_puzzle.project import ProjectError, load_project, save_project, state_signature
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


def test_stale_audio_slot_falls_back_to_auto_on_load(tmp_path: Path) -> None:
    """A pin saved with 4 slots must not survive a load of a 2-slot project as silence."""
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    clip_a.write_bytes(b"")
    clip_b.write_bytes(b"")
    state = AppState(layout=Layout.FOUR_SQUARE)
    state.set_slot(0, clip_a)
    state.set_slot(1, clip_b)
    state.audio_slot = 3
    dest = tmp_path / "job.vproj"
    save_project(state, dest)

    loaded = load_project(dest)
    loaded.set_layout(Layout.TWO_HORIZONTAL)
    assert loaded.audio_slot is None

    text = dest.read_text(encoding="utf-8").replace('"layout": "4square"', '"layout": "2h"')
    dest.write_text(text, encoding="utf-8")
    reloaded = load_project(dest)
    assert reloaded.audio_slot is None


def test_signature_of_a_loaded_project_matches_the_saved_one(tmp_path: Path) -> None:
    """Opening a file must not look like an edit.

    The window decides "unsaved changes" by comparing the live state against the
    signature taken at load time, so a round trip through the file has to be
    signature-neutral — paths included (they are stored relative to the project).
    """
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    clip_a.write_bytes(b"")
    clip_b.write_bytes(b"")
    state = AppState(layout=Layout.THREE_PYRAMID, resolution=720)
    state.set_slot(0, clip_a)
    state.set_slot(1, clip_b)
    state.slots[0].trim_start = 1.5
    state.set_cell_gap(6)
    dest = tmp_path / "job.vproj"
    save_project(state, dest)

    loaded = load_project(dest)

    assert state_signature(loaded, tmp_path) == state_signature(state, tmp_path)


def test_signature_follows_edits_and_ignores_the_playhead(tmp_path: Path) -> None:
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"")
    state = AppState(layout=Layout.TWO_VERTICAL)
    state.set_slot(0, clip)
    before = state_signature(state, tmp_path)

    state.set_playhead(4.0)
    assert state_signature(state, tmp_path) == before, "scrubbing is not an edit"

    state.slots[0].trim_start = 0.5
    assert state_signature(state, tmp_path) != before


def _payload(tmp_path: Path, **slot_fields) -> dict:
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"")
    slot = {"path": "a.mp4", **slot_fields}
    return {"version": 1, "mode": "puzzle", "layout": "2v", "slots": [slot]}


def _write(tmp_path: Path, payload: object, name: str = "bad.vproj") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "field,value",
    [
        ("trim_start", "oops"),
        ("trim_start", float("inf")),
        ("rotation", "x"),
        ("rotation", 720),
        ("crop", ["a", "b", "c", "d"]),
        ("crop", [float("nan"), 0.0, 0.5, 0.5]),
        ("mark_in", 5.0),
    ],
)
def test_load_rejects_invalid_slot_values(tmp_path: Path, field: str, value: object) -> None:
    payload = _payload(tmp_path)
    payload["slots"][0][field] = value
    with pytest.raises(ProjectError):
        load_project(_write(tmp_path, payload))


def test_load_rejects_a_newer_project_version(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["version"] = 99
    with pytest.raises(ProjectError, match="newer version"):
        load_project(_write(tmp_path, payload))


def test_load_rejects_non_list_slots(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["slots"] = "nope"
    with pytest.raises(ProjectError, match="slots"):
        load_project(_write(tmp_path, payload))


def test_load_requires_a_real_json_boolean(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["include_audio"] = "false"
    with pytest.raises(ProjectError, match="include_audio"):
        load_project(_write(tmp_path, payload))


def test_load_normalizes_parent_paths(tmp_path: Path) -> None:
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"")
    project_dir = tmp_path / "sub"
    project_dir.mkdir()
    payload = {
        "version": 1,
        "layout": "2v",
        "slots": [{"path": "../secret.mp4"}],
    }
    loaded = load_project(_write(project_dir, payload, "job.vproj"))
    assert loaded.slots[0].path == secret.resolve()


def test_load_skips_missing_files_and_reports_them(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "layout": "2v",
        "slots": [{"path": "gone.mp4"}],
    }
    missing: list[Path] = []
    loaded = load_project(_write(tmp_path, payload), missing=missing)

    assert loaded.slots[0].path is None
    assert missing == [(tmp_path / "gone.mp4").resolve()]
    assert not loaded.is_complete()


def test_load_ignores_slots_outside_the_active_scheme(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["slots"].extend([{"path": "a.mp4"}, {"path": "a.mp4"}])
    loaded = load_project(_write(tmp_path, payload))
    assert loaded.slots[0].path is not None
    assert loaded.slots[1].path is not None
    assert loaded.slots[2].path is None


def test_save_project_leaves_no_temporary_file(tmp_path: Path) -> None:
    state = AppState(layout=Layout.TWO_HORIZONTAL)
    dest = tmp_path / "job.vproj"
    save_project(state, dest)
    assert dest.exists()
    assert list(tmp_path.glob(".*.tmp")) == []
