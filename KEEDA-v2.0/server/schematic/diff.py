"""Structured diff for schematic objects.

Deliberately separate from `common/diff_engine.py`, which serves the PCB
engine. Schematic objects have different fields (lib_id, mirror, unit, sheet,
wire endpoints) and conflating the two would make both harder to reason about.

The comparison contract is the same on both sides, though: normalise first,
compare field by field, and never report a change nobody made.
"""
from __future__ import annotations

from typing import Any

# Per-object-type field lists. Only these are compared.
TRACKED_FIELDS = {
    "symbol": ("reference", "value", "lib_id", "position", "rotation",
               "mirror", "unit", "dnp", "footprint"),
    "wire": ("start", "end"),
    "local_label": ("text", "position", "rotation"),
    "global_label": ("text", "position", "rotation"),
    "hierarchical_label": ("text", "position", "rotation"),
    "junction": ("position",),
    "sheet": ("sheet_name", "sheet_file", "position"),
}

POSITION_FIELDS = ("position", "start", "end")
DP = 3


def _points_equal(a: Any, b: Any) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return a == b
    return (round(float(a.get("x", 0)), DP) == round(float(b.get("x", 0)), DP)
            and round(float(a.get("y", 0)), DP) == round(float(b.get("y", 0)), DP))


def values_equal(field: str, a: Any, b: Any) -> bool:
    if field in POSITION_FIELDS:
        return _points_equal(a, b)
    if field == "rotation":
        try:
            return round(float(a) % 360.0, DP) == round(float(b) % 360.0, DP)
        except (TypeError, ValueError):
            return a == b
    return a == b


def tracked_fields(object_type: str) -> tuple[str, ...]:
    return TRACKED_FIELDS.get(object_type, ("position",))


def diff_object(old: dict, new: dict) -> list[dict]:
    """Field-level changes between two states of one schematic object."""
    object_type = new.get("object_type") or old.get("object_type") or "symbol"
    changes: list[dict] = []
    for field in tracked_fields(object_type):
        if field not in new and field not in old:
            continue
        before, after = old.get(field), new.get(field)
        if not values_equal(field, before, after):
            changes.append({
                "operation": "modify",
                "domain": "schematic",
                "object_type": object_type,
                "uuid": new.get("uuid") or old.get("uuid"),
                "reference": new.get("reference") or old.get("reference", ""),
                "sheet": new.get("sheet") or old.get("sheet", "/"),
                "field": field,
                "old": before,
                "new": after,
            })
    return changes


def diff_snapshots(baseline: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    """Structured change list turning `baseline` into `current`."""
    changes: list[dict] = []

    for uuid, state in current.items():
        if uuid not in baseline:
            changes.append({
                "operation": "add",
                "domain": "schematic",
                "object_type": state.get("object_type", "symbol"),
                "uuid": uuid,
                "reference": state.get("reference", ""),
                "sheet": state.get("sheet", "/"),
                "state": state,
            })
        else:
            changes.extend(diff_object(baseline[uuid], state))

    for uuid, state in baseline.items():
        if uuid not in current:
            changes.append({
                "operation": "remove",
                "domain": "schematic",
                "object_type": state.get("object_type", "symbol"),
                "uuid": uuid,
                "reference": state.get("reference", ""),
                "sheet": state.get("sheet", "/"),
            })

    return changes


def summarise(change: dict) -> str:
    """One human-readable line, for the activity feed and notifications."""
    object_type = change.get("object_type", "object")
    reference = change.get("reference") or change.get("uuid", "?")[:8]
    noun = {
        "symbol": "symbol", "wire": "wire", "junction": "junction",
        "local_label": "label", "global_label": "global label",
        "hierarchical_label": "hierarchical label", "sheet": "sheet",
    }.get(object_type, object_type)

    operation = change.get("operation")
    if operation == "add":
        return f"added {noun} {reference}".strip()
    if operation == "remove":
        return f"removed {noun} {reference}".strip()

    field = change.get("field")
    old, new = change.get("old"), change.get("new")

    if field in POSITION_FIELDS:
        try:
            return f"moved {noun} {reference} to ({new['x']:.2f}, {new['y']:.2f})".strip()
        except (KeyError, TypeError, ValueError):
            return f"moved {noun} {reference}".strip()
    if field == "rotation":
        try:
            return f"rotated {reference} to {float(new):.0f} deg"
        except (TypeError, ValueError):
            return f"rotated {reference}"
    if field == "value":
        return f"changed {reference} value from {old} to {new}"
    if field == "reference":
        return f"renamed {old} to {new}"
    if field == "footprint":
        return f"set {reference} footprint to {new or '(none)'}"
    if field == "text":
        return f"changed label {old} to {new}"
    if field == "lib_id":
        return f"changed {reference} symbol to {new}"
    return f"changed {reference} {field}"
