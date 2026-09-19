"""Assemble the project hardware summary.

Runs entirely on the local machine: KiCad's own `kicad-cli` for connectivity,
direct file parsing for the board. **No network access and no AI service** -
verified by docs/HARDWARE_SUMMARY_TEST_PLAN.md with the internet disconnected.

The contract: anything that cannot be determined from the project is reported
as `None`, which the dashboard renders as "Not available". The analyzer never
fills a gap with a plausible guess.
"""
from __future__ import annotations

import logging
import os
import time

from server.project_analysis.component_analyzer import analyze_components
from server.project_analysis.net_analyzer import (
    detect_interfaces, detect_power, net_statistics,
)
from server.project_analysis.netlist import NetlistUnavailable, load_netlist
from server.project_analysis.pcb_analyzer import analyze_pcb, find_pcb
from server.schematic.parser import find_schematic

log = logging.getLogger("kicadlive.analysis.summary")

CACHE_TTL = 30.0          # seconds; analysis is cheap but not free


class ProjectAnalyzer:
    def __init__(self, project_dir: str | None = None):
        self.project_dir = project_dir
        self._cache: dict[str, tuple[float, dict]] = {}

    def summarise(self, project_dir: str | None = None, force: bool = False) -> dict:
        directory = project_dir or self.project_dir
        if not directory or not os.path.isdir(directory):
            return _empty("No project directory is configured on the server.")

        key = os.path.normcase(os.path.abspath(directory))
        cached = self._cache.get(key)
        if cached and not force and (time.time() - cached[0]) < CACHE_TTL:
            return cached[1]

        summary = _build(directory)
        self._cache[key] = (time.time(), summary)
        return summary

    def invalidate(self) -> None:
        self._cache.clear()


def _empty(reason: str) -> dict:
    return {
        "available": False, "reason": reason, "generated_at": time.time(),
        "offline": True, "schematic": None, "pcb": None,
    }


def _build(project_dir: str) -> dict:
    started = time.perf_counter()
    summary: dict = {
        "available": True,
        "generated_at": time.time(),
        "offline": True,          # nothing here ever touches the network
        "project_dir": project_dir,
        "project_name": os.path.basename(os.path.abspath(project_dir)),
        "warnings": [],
        "schematic": None,
        "pcb": None,
    }

    # ---------------------------------------------------------- schematic
    schematic_path = find_schematic(project_dir)
    if not schematic_path:
        summary["warnings"].append("No .kicad_sch found; schematic analysis unavailable.")
    else:
        summary["schematic_file"] = os.path.basename(schematic_path)
        try:
            netlist = load_netlist(schematic_path)
        except NetlistUnavailable as exc:
            summary["warnings"].append(f"Netlist unavailable: {exc}")
            log.warning("netlist export failed for %s: %s", schematic_path, exc)
        else:
            components = netlist["components"]
            nets = netlist["nets"]
            parts = analyze_components(components)
            interfaces = detect_interfaces(nets)
            power = detect_power(nets)

            summary["schematic"] = {
                "file": os.path.basename(schematic_path),
                "component_count": len(components),
                "net_count": len(nets),
                "sheets": netlist.get("sheets", []),
                "sheet_count": len(netlist.get("sheets", [])),
                "components": parts,
                "interfaces": interfaces,
                "power": power,
                "nets": net_statistics(nets),
                # The full component list drives the dashboard's object picker
                # for comments, so it is included rather than re-derived.
                "component_list": [
                    {"reference": c["reference"], "value": c["value"],
                     "footprint": c["footprint"], "library": c["library"],
                     "part": c["part"], "sheet": c["sheet"]}
                    for c in sorted(components, key=lambda c: _ref_key(c["reference"]))
                ],
            }

    # --------------------------------------------------------------- pcb
    pcb_path = find_pcb(project_dir)
    if not pcb_path:
        summary["warnings"].append("No .kicad_pcb found; board analysis unavailable.")
    else:
        try:
            summary["pcb"] = analyze_pcb(pcb_path)
        except (OSError, ValueError) as exc:
            summary["warnings"].append(f"Board analysis failed: {exc}")
            log.warning("pcb analysis failed for %s: %s", pcb_path, exc)

    summary["headline"] = _headline(summary)
    summary["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return summary


def _ref_key(reference: str):
    """Sort R1, R2, R10 in the order a human expects."""
    import re
    match = re.match(r"^([A-Za-z]+)(\d*)", reference or "")
    if not match:
        return (reference or "", 0)
    return (match.group(1), int(match.group(2) or 0))


def _headline(summary: dict) -> list[dict]:
    """The short 'what is this board' list, with `None` for unknowns."""
    schematic = summary.get("schematic")
    pcb = summary.get("pcb")
    lines: list[dict] = []

    def add(label, value, evidence=None):
        lines.append({"label": label, "value": value, "evidence": evidence})

    if schematic:
        mcus = schematic["components"]["microcontrollers"]
        if mcus:
            add("Microcontroller",
                ", ".join(sorted({m["family"] for m in mcus})),
                ", ".join(f"{m['reference']} ({m['part']})" for m in mcus[:4]))
        else:
            add("Microcontroller", None, "no recognised MCU part number in the schematic")

        rails = schematic["power"]["rails"]
        if rails:
            add("Power rails", ", ".join(r["leaf"] for r in rails[:5]),
                f"{len(rails)} power net(s)")
        else:
            add("Power rails", None, "no nets matching power-rail naming")

        interfaces = schematic["interfaces"]
        if interfaces:
            add("Interfaces", ", ".join(i["name"] for i in interfaces),
                "; ".join(f"{i['name']}: {', '.join(i['evidence'][:3])}" for i in interfaces))
        else:
            add("Interfaces", None, "no interface signal names or pin functions found")

        add("Components", f"{schematic['component_count']} components, "
                          f"{schematic['components']['distinct_values']} distinct values")
        add("Nets", str(schematic["net_count"]))
    else:
        add("Schematic", None, "no schematic analysed")

    if pcb:
        count = pcb.get("copper_layer_count")
        add("Board stack-up", f"{count}-layer" if count else None,
            ", ".join(pcb.get("copper_layers", [])) or None)
        dimensions = pcb.get("dimensions_mm")
        if dimensions:
            add("Board size", f"{dimensions['width']} x {dimensions['height']} mm",
                "measured from the Edge.Cuts outline")
        else:
            extent = pcb.get("component_extent_mm")
            add("Board size", None,
                (f"no Edge.Cuts outline; components span "
                 f"{extent['width']} x {extent['height']} mm" if extent
                 else "no board outline defined"))
        add("Footprints", str(pcb.get("footprint_count", 0)))
        add("Routing", f"{pcb.get('track_segment_count', 0)} track segments, "
                       f"{pcb.get('via_count', 0)} vias, {pcb.get('zone_count', 0)} zones")
    else:
        add("Board", None, "no board analysed")

    return lines
