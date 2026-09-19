"""Extract board facts from a .kicad_pcb file.

Reads the file directly rather than the live board, so the summary works with
no KiCad running - a project manager looking at the dashboard should not need
pcbnew open on their machine.
"""
from __future__ import annotations

import logging
import os

from common.sexpr import as_float, child, children, parse_file, value, values

log = logging.getLogger("kicadlive.analysis.pcb")

# Layers that indicate copper; used to report the real layer count.
COPPER_SUFFIX = ".Cu"


def analyze_pcb(pcb_path: str) -> dict:
    """Board facts. Anything not determinable is simply absent from the result."""
    if not os.path.exists(pcb_path):
        raise FileNotFoundError(pcb_path)

    root = parse_file(pcb_path)
    if not root or root[0] != "kicad_pcb":
        raise ValueError(f"{pcb_path}: not a kicad_pcb file")

    result: dict = {"file": os.path.basename(pcb_path)}

    # --- layers -----------------------------------------------------------
    layer_names: list[str] = []
    layers_node = child(root, "layers")
    if layers_node is not None:
        for entry in layers_node[1:]:
            if isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], str):
                layer_names.append(entry[1])
    copper = [n for n in layer_names if n.endswith(COPPER_SUFFIX)]
    result["layers"] = layer_names
    result["copper_layers"] = copper
    result["copper_layer_count"] = len(copper)

    # --- objects ----------------------------------------------------------
    footprints = children(root, "footprint")
    result["footprint_count"] = len(footprints)
    result["via_count"] = len(children(root, "via"))
    result["zone_count"] = len(children(root, "zone"))
    result["track_segment_count"] = len(children(root, "segment"))
    result["arc_segment_count"] = len(children(root, "arc"))

    nets = children(root, "net")
    # net 0 is KiCad's unconnected pseudo-net.
    real_nets = [n for n in nets if len(n) > 1 and str(n[1]) != "0"]
    result["net_count"] = len(real_nets)
    result["net_names"] = [n[2] for n in real_nets
                           if len(n) > 2 and isinstance(n[2], str) and n[2]][:200]

    # --- placement --------------------------------------------------------
    placed, layer_tally = [], {}
    for footprint in footprints:
        position = values(footprint, "at") or []
        reference = ""
        for prop in children(footprint, "property"):
            if len(prop) >= 3 and prop[1] == "Reference":
                reference = prop[2]
                break
        layer = value(footprint, "layer", default="")
        layer_tally[layer] = layer_tally.get(layer, 0) + 1
        if position:
            placed.append({
                "reference": reference,
                "x": round(as_float(position[0] if len(position) > 0 else 0), 3),
                "y": round(as_float(position[1] if len(position) > 1 else 0), 3),
                "layer": layer,
            })
    result["footprints_by_layer"] = layer_tally
    result["placed_count"] = len(placed)

    # --- board outline ----------------------------------------------------
    dimensions = _board_dimensions(root)
    if dimensions:
        result["dimensions_mm"] = dimensions
    else:
        # Fall back to the placement extent, clearly labelled as such.
        if placed:
            xs = [p["x"] for p in placed]
            ys = [p["y"] for p in placed]
            result["component_extent_mm"] = {
                "width": round(max(xs) - min(xs), 2),
                "height": round(max(ys) - min(ys), 2),
            }

    title = child(root, "title_block")
    if title is not None:
        result["title"] = value(title, "title", default="") or ""
        result["revision"] = value(title, "rev", default="") or ""

    result["generator"] = value(root, "generator", default="") or ""
    result["version"] = value(root, "version", default="") or ""
    return result


def _board_dimensions(root: list) -> dict | None:
    """Bounding box of Edge.Cuts geometry, in millimetres.

    Only geometry actually on Edge.Cuts counts - that is the board outline a
    fabricator would cut to. Returns None when there is no outline, so the
    caller can say "Not available" instead of inventing a size.
    """
    xs: list[float] = []
    ys: list[float] = []

    def consider(node: list) -> None:
        if value(node, "layer") != "Edge.Cuts":
            return
        for keyword in ("start", "end", "center", "mid", "at"):
            point = values(node, keyword)
            if point and len(point) >= 2:
                xs.append(as_float(point[0]))
                ys.append(as_float(point[1]))
        pts = child(node, "pts")
        if pts is not None:
            for xy in children(pts, "xy"):
                if len(xy) >= 3:
                    xs.append(as_float(xy[1]))
                    ys.append(as_float(xy[2]))

    for keyword in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve"):
        for node in children(root, keyword):
            consider(node)

    if len(xs) < 2 or len(ys) < 2:
        return None
    width = round(max(xs) - min(xs), 2)
    height = round(max(ys) - min(ys), 2)
    if width <= 0 or height <= 0:
        return None
    return {"width": width, "height": height}


def find_pcb(project_dir: str) -> str | None:
    """The project's .kicad_pcb, matching the .kicad_pro name when possible."""
    if not os.path.isdir(project_dir):
        return None
    boards = [f for f in os.listdir(project_dir) if f.endswith(".kicad_pcb")]
    if not boards:
        return None
    for project_file in os.listdir(project_dir):
        if project_file.endswith(".kicad_pro"):
            expected = project_file[: -len(".kicad_pro")] + ".kicad_pcb"
            if expected in boards:
                return os.path.join(project_dir, expected)
    if len(boards) == 1:
        return os.path.join(project_dir, boards[0])
    return None
