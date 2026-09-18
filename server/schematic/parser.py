"""Turn a .kicad_sch file into KiCad Live's normalised schematic state.

Kept strictly separate from the PCB engine: schematic and board objects are
genuinely different and merging them into one parser would make both worse.

Everything produced here is keyed on KiCad's own UUIDs. Those are stable
across saves - PCB footprints reference schematic symbol UUIDs through
`(path "/<uuid>")`, so KiCad cannot renumber them without breaking its own
schematic-to-board link.
"""
from __future__ import annotations

import logging
import os

from common.sexpr import (
    as_float, child, children, parse_file, properties, value, values,
)

log = logging.getLogger("kicadlive.schematic.parser")

# Fields of a schematic symbol that KiCad Live tracks.
SYMBOL_FIELDS = ("reference", "value", "lib_id", "position", "rotation",
                 "mirror", "unit", "dnp", "footprint")

LABEL_KINDS = {
    "label": "local_label",
    "global_label": "global_label",
    "hierarchical_label": "hierarchical_label",
}


def _xy(node: list, keyword: str = "at") -> dict[str, float]:
    raw = values(node, keyword) or []
    return {"x": round(as_float(raw[0] if len(raw) > 0 else 0), 3),
            "y": round(as_float(raw[1] if len(raw) > 1 else 0), 3)}


def _rotation(node: list) -> float:
    raw = values(node, "at") or []
    return round(as_float(raw[2] if len(raw) > 2 else 0) % 360.0, 3)


def _points(node: list) -> list[dict[str, float]]:
    pts = child(node, "pts")
    if pts is None:
        return []
    out = []
    for point in children(pts, "xy"):
        out.append({"x": round(as_float(point[1] if len(point) > 1 else 0), 3),
                    "y": round(as_float(point[2] if len(point) > 2 else 0), 3)})
    return out


def _uuid_of(node: list) -> str | None:
    found = value(node, "uuid")
    return found if isinstance(found, str) else None


class SchematicSheet:
    """One parsed .kicad_sch file."""

    def __init__(self, path: str, sheet_name: str = "/", sheet_uuid: str = ""):
        self.path = path
        self.sheet_name = sheet_name
        self.sheet_uuid = sheet_uuid
        self.objects: dict[str, dict] = {}
        self.child_sheets: list[dict] = []


def parse_sheet(path: str, sheet_name: str = "/", sheet_uuid: str = "") -> SchematicSheet:
    """Parse one schematic file into normalised objects."""
    sheet = SchematicSheet(path, sheet_name, sheet_uuid)
    root = parse_file(path)

    if not root or root[0] != "kicad_sch":
        raise ValueError(f"{path}: not a kicad_sch file (root={root[0] if root else '?'})")

    for node in root:
        if not isinstance(node, list) or not node:
            continue
        keyword = node[0]

        # `lib_symbols` holds library DEFINITIONS, not placed symbols. Only
        # nodes carrying a lib_id at the top level are real instances.
        if keyword == "symbol" and child(node, "lib_id") is not None:
            obj = _parse_symbol(node, sheet_name)
        elif keyword == "wire":
            obj = _parse_wire(node, sheet_name)
        elif keyword in LABEL_KINDS:
            obj = _parse_label(node, keyword, sheet_name)
        elif keyword == "junction":
            obj = _parse_junction(node, sheet_name)
        elif keyword == "sheet":
            obj = _parse_sheet_symbol(node, sheet_name)
            if obj is not None:
                sheet.child_sheets.append(obj)
        else:
            continue

        if obj is not None:
            sheet.objects[obj["uuid"]] = obj

    return sheet


def _parse_symbol(node: list, sheet_name: str) -> dict | None:
    uuid = _uuid_of(node)
    if not uuid:
        return None
    props = properties(node)
    mirror = value(node, "mirror")
    return {
        "uuid": uuid,
        "domain": "schematic",
        "object_type": "symbol",
        "sheet": sheet_name,
        "reference": props.get("Reference", ""),
        "value": props.get("Value", ""),
        "footprint": props.get("Footprint", ""),
        "lib_id": value(node, "lib_id", default=""),
        "position": _xy(node),
        "rotation": _rotation(node),
        "mirror": mirror if isinstance(mirror, str) else "",
        "unit": value(node, "unit", default="1"),
        "dnp": value(node, "dnp", default="no"),
    }


def _parse_wire(node: list, sheet_name: str) -> dict | None:
    uuid = _uuid_of(node)
    if not uuid:
        return None
    pts = _points(node)
    return {
        "uuid": uuid,
        "domain": "schematic",
        "object_type": "wire",
        "sheet": sheet_name,
        "reference": "",
        "points": pts,
        "start": pts[0] if pts else {"x": 0.0, "y": 0.0},
        "end": pts[-1] if pts else {"x": 0.0, "y": 0.0},
    }


def _parse_label(node: list, keyword: str, sheet_name: str) -> dict | None:
    uuid = _uuid_of(node)
    if not uuid:
        return None
    text = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
    return {
        "uuid": uuid,
        "domain": "schematic",
        "object_type": LABEL_KINDS[keyword],
        "sheet": sheet_name,
        "reference": text,
        "text": text,
        "position": _xy(node),
        "rotation": _rotation(node),
    }


def _parse_junction(node: list, sheet_name: str) -> dict | None:
    uuid = _uuid_of(node)
    if not uuid:
        return None
    return {
        "uuid": uuid,
        "domain": "schematic",
        "object_type": "junction",
        "sheet": sheet_name,
        "reference": "",
        "position": _xy(node),
    }


def _parse_sheet_symbol(node: list, sheet_name: str) -> dict | None:
    """A hierarchical sheet placed inside another sheet."""
    uuid = _uuid_of(node)
    if not uuid:
        return None
    props = properties(node)
    name = props.get("Sheetname") or props.get("Sheet name") or ""
    filename = props.get("Sheetfile") or props.get("Sheet file") or ""
    return {
        "uuid": uuid,
        "domain": "schematic",
        "object_type": "sheet",
        "sheet": sheet_name,
        "reference": name,
        "sheet_name": name,
        "sheet_file": filename,
        "position": _xy(node),
    }


def parse_schematic(root_path: str, max_sheets: int = 64) -> dict[str, dict]:
    """Parse a schematic and every hierarchical sheet it references.

    Returns ``{uuid: object}`` across the whole hierarchy. Child sheets are
    resolved relative to the root file and visited breadth-first; a sheet is
    visited once even if instantiated twice, and traversal is bounded so a
    circular reference cannot hang the server.
    """
    root_path = os.path.abspath(root_path)
    base_dir = os.path.dirname(root_path)

    objects: dict[str, dict] = {}
    seen_files: set[str] = set()
    queue: list[tuple[str, str, str]] = [(root_path, "/", "")]

    while queue and len(seen_files) < max_sheets:
        path, sheet_name, sheet_uuid = queue.pop(0)
        key = os.path.normcase(os.path.abspath(path))
        if key in seen_files:
            continue
        seen_files.add(key)

        try:
            sheet = parse_sheet(path, sheet_name, sheet_uuid)
        except (OSError, ValueError) as exc:
            log.warning("could not parse sheet %s: %s", path, exc)
            continue

        objects.update(sheet.objects)

        for reference in sheet.child_sheets:
            filename = reference.get("sheet_file") or ""
            if not filename:
                continue
            # Sheet files are named relative to their parent. Reject anything
            # that tries to escape the project directory.
            candidate = os.path.abspath(os.path.join(os.path.dirname(path), filename))
            if os.path.commonpath([base_dir, candidate]) != base_dir:
                log.warning("refusing sheet outside the project directory: %s", filename)
                continue
            if not os.path.exists(candidate):
                log.warning("hierarchical sheet file missing: %s", candidate)
                continue
            child_name = f"{sheet_name.rstrip('/')}/{reference.get('sheet_name') or 'sheet'}"
            queue.append((candidate, child_name, reference["uuid"]))

    return objects


def find_schematic(project_dir: str) -> str | None:
    """The root .kicad_sch in a project directory, if there is one.

    A project's root schematic shares its name with the .kicad_pro; failing
    that, the only .kicad_sch present is used.
    """
    if not os.path.isdir(project_dir):
        return None
    schematics = [f for f in os.listdir(project_dir) if f.endswith(".kicad_sch")]
    if not schematics:
        return None

    for project_file in os.listdir(project_dir):
        if project_file.endswith(".kicad_pro"):
            expected = project_file[: -len(".kicad_pro")] + ".kicad_sch"
            if expected in schematics:
                return os.path.join(project_dir, expected)

    if len(schematics) == 1:
        return os.path.join(project_dir, schematics[0])
    return None
