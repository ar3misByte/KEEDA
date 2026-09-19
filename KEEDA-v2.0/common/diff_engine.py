"""Structured diff for board objects.

Shared by the server and the agent so that "what changed" has exactly one
definition in the system.

A *snapshot* is ``{uuid: object_state}`` where object_state is a plain dict:

    {"uuid": "...", "reference": "R1", "object_type": "footprint",
     "position": {"x": 60.0, "y": 50.0}, "rotation": 0.0,
     "layer": "F.Cu", "value": "10k"}

Coordinates are millimetres, already rounded, so float noise cannot
manufacture a change that nobody made.
"""
from __future__ import annotations

from typing import Any

from common.protocol import SYNCED_FIELDS

# KiCad stores coordinates in nanometres. 3 dp of a millimetre is 1 micrometre,
# which is finer than any real board tolerance and coarse enough that repeated
# reads of an unchanged object always compare equal.
POSITION_DP = 3
ROTATION_DP = 3


def nm_to_mm(nanometres: int) -> float:
    return round(nanometres / 1_000_000.0, POSITION_DP)


def mm_to_nm(millimetres: float) -> int:
    return int(round(millimetres * 1_000_000.0))


def normalise_position(x_mm: float, y_mm: float) -> dict[str, float]:
    return {"x": round(float(x_mm), POSITION_DP), "y": round(float(y_mm), POSITION_DP)}


def normalise_rotation(degrees: float) -> float:
    # Fold into [0, 360) so 0 and 360 are never reported as a change.
    return round(float(degrees) % 360.0, ROTATION_DP)


def values_equal(field: str, a: Any, b: Any) -> bool:
    """Compare one field's values the way the diff engine defines equality."""
    if field == "position":
        if not isinstance(a, dict) or not isinstance(b, dict):
            return a == b
        return (round(float(a.get("x", 0)), POSITION_DP) == round(float(b.get("x", 0)), POSITION_DP)
                and round(float(a.get("y", 0)), POSITION_DP) == round(float(b.get("y", 0)), POSITION_DP))
    if field == "rotation":
        try:
            return normalise_rotation(a) == normalise_rotation(b)
        except (TypeError, ValueError):
            return a == b
    return a == b


def diff_object(old: dict, new: dict) -> list[dict]:
    """Field-level changes between two states of the same object."""
    changes: list[dict] = []
    for field in SYNCED_FIELDS:
        if field not in new:
            continue
        old_value = old.get(field)
        new_value = new.get(field)
        if not values_equal(field, old_value, new_value):
            changes.append({
                "operation": "modify",
                "object_type": new.get("object_type", "footprint"),
                "uuid": new.get("uuid") or old.get("uuid"),
                "reference": new.get("reference") or old.get("reference", ""),
                "field": field,
                "old": old_value,
                "new": new_value,
            })
    return changes


def diff_snapshots(baseline: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    """Structured change list turning `baseline` into `current`.

    Emits `add` for objects only in current, `remove` for objects only in
    baseline, and one `modify` per changed field otherwise.
    """
    changes: list[dict] = []

    for uuid, state in current.items():
        if uuid not in baseline:
            changes.append({
                "operation": "add",
                "object_type": state.get("object_type", "footprint"),
                "uuid": uuid,
                "reference": state.get("reference", ""),
                "state": state,
            })
        else:
            changes.extend(diff_object(baseline[uuid], state))

    for uuid, state in baseline.items():
        if uuid not in current:
            changes.append({
                "operation": "remove",
                "object_type": state.get("object_type", "footprint"),
                "uuid": uuid,
                "reference": state.get("reference", ""),
            })

    return changes


def apply_change(snapshot: dict[str, dict], change: dict) -> None:
    """Apply one structured change to a snapshot, in place.

    Used to keep the agent's baseline and the server's authoritative state in
    step without re-reading the board.
    """
    operation = change.get("operation")
    uuid = change.get("uuid")
    if not uuid:
        return

    if operation == "add":
        state = dict(change.get("state") or {})
        state["uuid"] = uuid
        snapshot[uuid] = state
    elif operation == "remove":
        snapshot.pop(uuid, None)
    elif operation == "modify":
        field = change.get("field")
        if field not in SYNCED_FIELDS:
            return
        target = snapshot.setdefault(uuid, {
            "uuid": uuid,
            "reference": change.get("reference", ""),
            "object_type": change.get("object_type", "footprint"),
        })
        target[field] = change.get("new")


def summarise(change: dict) -> str:
    """One short human-readable line, for the activity feed."""
    reference = change.get("reference") or change.get("uuid", "?")[:8]
    operation = change.get("operation")
    if operation == "add":
        return f"added {reference}"
    if operation == "remove":
        return f"removed {reference}"

    field = change.get("field")
    if field == "position":
        new = change.get("new") or {}
        try:
            return f"moved {reference} to ({new['x']:.2f}, {new['y']:.2f})"
        except (KeyError, TypeError, ValueError):
            return f"moved {reference}"
    if field == "rotation":
        try:
            return f"rotated {reference} to {float(change.get('new', 0)):.0f} deg"
        except (TypeError, ValueError):
            return f"rotated {reference}"
    if field == "layer":
        return f"moved {reference} to layer {change.get('new')}"
    return f"changed {reference} {field}"
