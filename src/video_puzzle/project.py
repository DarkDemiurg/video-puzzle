from __future__ import annotations

import json
from pathlib import Path

from video_puzzle.encode import DEFAULT_ENCODER, DEFAULT_QUALITY, EncodeQuality, EncoderKind
from video_puzzle.layout import MAX_SLOTS, AppMode, Layout
from video_puzzle.state import AppState, Slot, is_video_file

PROJECT_VERSION = 1


class ProjectError(ValueError):
    """Project file could not be read or written."""


def _slot_dump(slot: Slot, project_dir: Path) -> dict:
    path = None
    if slot.path is not None:
        try:
            path = str(slot.path.relative_to(project_dir))
        except ValueError:
            path = str(slot.path)
    crop = None
    if slot.crop is not None:
        crop = list(slot.crop)
    return {
        "path": path,
        "trim_start": slot.trim_start,
        "mark_in": slot.mark_in,
        "mark_out": slot.mark_out,
        "rotation": slot.rotation,
        "crop": crop,
    }


def _resolve_path(raw: str | None, project_dir: Path) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = project_dir / path
    return path


def dump_project(state: AppState, project_path: Path) -> dict:
    project_dir = project_path.parent
    return {
        "version": PROJECT_VERSION,
        "mode": str(state.mode),
        "layout": str(state.layout),
        "wall_rows": state.wall_rows,
        "wall_cols": state.wall_cols,
        "resolution": state.resolution,
        "quality": str(state.quality),
        "encoder": str(state.encoder),
        "include_audio": state.include_audio,
        "audio_slot": state.audio_slot,
        "normalize_audio": state.normalize_audio,
        "cell_gap": state.cell_gap,
        "range_enabled": state.range_enabled,
        "range_start": state.range_start,
        "range_end": state.range_end,
        "slots": [_slot_dump(slot, project_dir) for slot in state.slots[:MAX_SLOTS]],
    }


def save_project(state: AppState, project_path: Path) -> None:
    project_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dump_project(state, project_path)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    project_path.write_text(text, encoding="utf-8")


def load_project(project_path: Path) -> AppState:
    try:
        data = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"Не удалось прочитать проект: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError("Некорректный файл проекта")
    state = AppState()
    try:
        state.set_mode(AppMode(data.get("mode", AppMode.PUZZLE)))
        state.set_layout(Layout(data.get("layout", Layout.FOUR_SQUARE)))
        state.set_wall_grid(int(data.get("wall_rows", 2)), int(data.get("wall_cols", 2)))
        state.set_resolution(int(data.get("resolution", 1080)))
        state.set_quality(EncodeQuality(data.get("quality", DEFAULT_QUALITY)))
        state.set_encoder(EncoderKind(data.get("encoder", DEFAULT_ENCODER)))
        state.include_audio = bool(data.get("include_audio", True))
        audio_slot = data.get("audio_slot")
        state.audio_slot = int(audio_slot) if audio_slot is not None else None
        state.normalize_audio = bool(data.get("normalize_audio", False))
        state.set_cell_gap(int(data.get("cell_gap", 0)))
        state.set_range_enabled(bool(data.get("range_enabled", False)))
        state.set_output_range(float(data.get("range_start", 0.0)), data.get("range_end"))
    except (TypeError, ValueError) as exc:
        raise ProjectError(f"Некорректные поля проекта: {exc}") from exc

    project_dir = project_path.parent
    for index, raw in enumerate(data.get("slots") or []):
        if index >= MAX_SLOTS or not isinstance(raw, dict):
            break
        path = _resolve_path(raw.get("path"), project_dir)
        if path is not None and is_video_file(path):
            state.set_slot(index, path)
            slot = state.slots[index]
            slot.trim_start = float(raw.get("trim_start") or 0.0)
            mark_in = raw.get("mark_in")
            mark_out = raw.get("mark_out")
            if mark_in is not None and mark_out is not None:
                slot.set_marks(float(mark_in), float(mark_out))
            slot.rotation = int(raw.get("rotation") or 0) % 360
            crop = raw.get("crop")
            if isinstance(crop, list) and len(crop) == 4:
                slot.crop = (float(crop[0]), float(crop[1]), float(crop[2]), float(crop[3]))
    return state
