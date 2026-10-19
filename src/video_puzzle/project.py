from __future__ import annotations

import json
import math
import os
from pathlib import Path

from video_puzzle.encode import DEFAULT_ENCODER, DEFAULT_QUALITY, EncodeQuality, EncoderKind
from video_puzzle.i18n import tr
from video_puzzle.layout import MAX_SLOTS, AppMode, Layout, canvas_size
from video_puzzle.state import AppState, Slot, is_video_file

PROJECT_VERSION = 1


class ProjectError(ValueError):
    """Project file could not be read or written.

    ``detail`` keeps the technical reason (an exception from ``json``/``os``) for the
    log.  The exception text itself is localized and safe to show in the UI.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


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


def _resolve_path(raw: str, project_dir: Path) -> Path:
    """Normalise a project path so ``..`` and ``~`` cannot hide a different file."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def _dump(state: AppState, project_dir: Path) -> dict:
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
    """Write the project atomically: a crash must not leave a truncated ``.vproj``."""
    payload = _dump(state, project_path.parent)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = project_path.with_name(f".{project_path.name}.tmp")
    try:
        project_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, project_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ProjectError(tr("Could not save the project."), detail=str(exc)) from exc


def state_signature(state: AppState, project_dir: Path) -> str:
    """Canonical text of everything a project file keeps for ``state``.

    The window remembers this text for the state it loaded or saved; anything that
    changes it means the arrangement on screen no longer matches the file. That is how
    opening another project can warn about unsaved edits instead of dropping them, and
    why the comparison is made against the same directory the file was read from.
    """
    return json.dumps(_dump(state, project_dir), ensure_ascii=False, sort_keys=True)


def _parse_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ProjectError(tr("Field '{field}' must be an integer", field=field))
    try:
        return int(value)
    except ValueError as exc:
        raise ProjectError(tr("Field '{field}' must be an integer", field=field)) from exc


def _parse_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ProjectError(tr("Field '{field}' must be a number", field=field))
    try:
        number = float(value)
    except ValueError as exc:
        raise ProjectError(tr("Field '{field}' must be a number", field=field)) from exc
    if not math.isfinite(number):
        raise ProjectError(tr("Field '{field}' must be a finite number", field=field))
    return number


def _parse_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProjectError(tr("Field '{field}' must be true or false", field=field))
    return value


def _parse_crop(value: object, index: int) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ProjectError(tr("Slot {slot}: crop must be a list of four numbers", slot=index + 1))
    x, y, width, height = (
        _parse_float(item, f"crop[{position}]") for position, item in enumerate(value)
    )
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise ProjectError(tr("Slot {slot}: crop is outside the frame", slot=index + 1))
    if not (0.0 < width <= 1.0 and 0.0 < height <= 1.0):
        raise ProjectError(tr("Slot {slot}: crop must have a positive size", slot=index + 1))
    if x + width > 1.0 + 1e-6 or y + height > 1.0 + 1e-6:
        raise ProjectError(tr("Slot {slot}: crop is outside the frame", slot=index + 1))
    return x, y, width, height


def load_project(project_path: Path, *, missing: list[Path] | None = None) -> AppState:
    """Load and validate a project file.

    Every value is checked before it reaches ``AppState``: a damaged or foreign file
    used to raise raw ``ValueError``/``AttributeError`` (or, worse, pass NaN/Inf into
    ffmpeg filters). Files are normalised with ``resolve()`` and missing media is
    skipped, while callers can collect the paths in ``missing`` for a warning.
    """
    try:
        data = json.loads(project_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProjectError(tr("Could not read the project file."), detail=str(exc)) from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProjectError(tr("Invalid project file"), detail=str(exc)) from exc
    if not isinstance(data, dict):
        raise ProjectError(tr("Invalid project file"))

    version = _parse_int(data.get("version", PROJECT_VERSION), "version")
    if version < 1:
        raise ProjectError(tr("Unsupported project version: {version}", version=version))
    if version > PROJECT_VERSION:
        raise ProjectError(
            tr(
                "The project was created by a newer version of Video Puzzle "
                "(version {version}; supported: {supported})",
                version=version,
                supported=PROJECT_VERSION,
            )
        )

    state = AppState()
    try:
        state.set_mode(AppMode(data.get("mode", AppMode.PUZZLE)))
        state.set_layout(Layout(data.get("layout", Layout.FOUR_SQUARE)))
        state.set_wall_grid(
            _parse_int(data.get("wall_rows", 2), "wall_rows"),
            _parse_int(data.get("wall_cols", 2), "wall_cols"),
        )
        resolution = _parse_int(data.get("resolution", 1080), "resolution")
        canvas_size(resolution)
        state.set_resolution(resolution)
        state.set_quality(EncodeQuality(data.get("quality", DEFAULT_QUALITY)))
        state.set_encoder(EncoderKind(data.get("encoder", DEFAULT_ENCODER)))
        state.include_audio = _parse_bool(data.get("include_audio", True), "include_audio")
        audio_slot = data.get("audio_slot")
        if audio_slot is not None:
            try:
                state.set_audio_slot(_parse_int(audio_slot, "audio_slot"))
            except IndexError:
                # Saved with a wider scheme than the one being loaded — silently keeping
                # that pin would produce a script without audio, so fall back to auto.
                state.audio_slot = None
        state.normalize_audio = _parse_bool(data.get("normalize_audio", False), "normalize_audio")
        state.set_cell_gap(_parse_int(data.get("cell_gap", 0), "cell_gap"))
        state.set_range_enabled(_parse_bool(data.get("range_enabled", False), "range_enabled"))
        range_start = _parse_float(data.get("range_start", 0.0), "range_start")
        if range_start < 0:
            raise ProjectError(tr("Field 'range_start' cannot be negative"))
        range_end_raw = data.get("range_end")
        range_end = None if range_end_raw is None else _parse_float(range_end_raw, "range_end")
        if range_end is not None and range_end <= range_start:
            raise ProjectError(tr("The fragment end must be greater than its start"))
        state.set_output_range(range_start, range_end)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ProjectError):
            raise
        raise ProjectError(tr("Invalid project fields"), detail=str(exc)) from exc

    raw_slots = data.get("slots")
    if raw_slots is None:
        raw_slots = []
    if not isinstance(raw_slots, list):
        raise ProjectError(tr("Field 'slots' must be a list"))

    project_dir = project_path.parent
    for index, raw in enumerate(raw_slots[:MAX_SLOTS]):
        if not isinstance(raw, dict):
            raise ProjectError(tr("Slot {slot}: expected a JSON object", slot=index + 1))
        # Slots outside the active scheme are not restored: the UI has no widgets for
        # them, and keeping them only produced a state that disagreed with the screen.
        if index >= state.active_count:
            continue
        raw_path = raw.get("path")
        if raw_path is None:
            continue
        if not isinstance(raw_path, str):
            raise ProjectError(tr("Slot {slot}: path must be a string", slot=index + 1))
        path = _resolve_path(raw_path, project_dir)
        if not is_video_file(path):
            raise ProjectError(
                tr(
                    "Slot {slot}: unsupported file type {suffix}",
                    slot=index + 1,
                    suffix=path.suffix,
                )
            )
        if not path.is_file():
            if missing is not None:
                missing.append(path)
            continue

        state.set_slot(index, path)
        slot = state.slots[index]
        slot.trim_start = _parse_float(raw.get("trim_start", 0.0), f"slot {index + 1} trim_start")
        if slot.trim_start < 0:
            raise ProjectError(tr("Slot {slot}: trim_start cannot be negative", slot=index + 1))
        mark_in_raw = raw.get("mark_in")
        mark_out_raw = raw.get("mark_out")
        if (mark_in_raw is None) != (mark_out_raw is None):
            raise ProjectError(
                tr("Slot {slot}: mark_in and mark_out must be set together", slot=index + 1)
            )
        if mark_in_raw is not None:
            mark_in = _parse_float(mark_in_raw, f"slot {index + 1} mark_in")
            mark_out = _parse_float(mark_out_raw, f"slot {index + 1} mark_out")
            if mark_in < 0 or mark_out <= mark_in:
                raise ProjectError(tr("Slot {slot}: invalid mark range", slot=index + 1))
            slot.set_marks(mark_in, mark_out)

        rotation = _parse_int(raw.get("rotation", 0), f"slot {index + 1} rotation")
        if not 0 <= rotation < 360:
            raise ProjectError(
                tr(
                    "Slot {slot}: rotation must be between 0 and 359",
                    slot=index + 1,
                )
            )
        slot.rotation = rotation
        slot.crop = _parse_crop(raw.get("crop"), index)
    return state
