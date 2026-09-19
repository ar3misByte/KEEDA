"""Extract the project's connectivity, entirely offline.

Connectivity is not re-derived here. KiCad's own `kicad-cli` exports the
netlist its schematic engine computed, which is both authoritative and fast
(measured: 0.37 s for a 63-component, 111-net project). Reimplementing wire
tracing would be slower to write and less correct.

No network access, no API keys, no AI service. `kicad-cli` ships with KiCad.
"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

log = logging.getLogger("kicadlive.analysis.netlist")

EXPORT_TIMEOUT = 120


def find_kicad_cli() -> str | None:
    """Locate kicad-cli without assuming a single install path."""
    from shutil import which
    found = which("kicad-cli")
    if found:
        return found

    patterns = []
    if sys.platform == "win32":
        patterns = [r"C:\Program Files\KiCad\*\bin\kicad-cli.exe",
                    r"C:\Program Files (x86)\KiCad\*\bin\kicad-cli.exe"]
    elif sys.platform == "darwin":
        patterns = ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    else:
        patterns = ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]

    candidates: list[str] = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    # Highest KiCad version wins.
    candidates.sort()
    return candidates[-1]


class NetlistUnavailable(RuntimeError):
    """The netlist could not be exported; the caller reports 'Not available'."""


def export_netlist_xml(schematic_path: str, cli_path: str | None = None) -> str:
    """Run kicad-cli and return the netlist XML text."""
    cli = cli_path or find_kicad_cli()
    if not cli:
        raise NetlistUnavailable("kicad-cli was not found on this machine")
    if not os.path.exists(schematic_path):
        raise NetlistUnavailable(f"schematic not found: {schematic_path}")

    with tempfile.TemporaryDirectory(prefix="kicadlive_net_") as tmp:
        output = os.path.join(tmp, "netlist.xml")
        command = [cli, "sch", "export", "netlist", "--format", "kicadxml",
                   "-o", output, schematic_path]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=EXPORT_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise NetlistUnavailable("kicad-cli timed out exporting the netlist") from exc
        except OSError as exc:
            raise NetlistUnavailable(f"could not run kicad-cli: {exc}") from exc

        if result.returncode != 0 or not os.path.exists(output):
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise NetlistUnavailable(
                "kicad-cli failed: " + (detail[-1] if detail else f"exit {result.returncode}"))

        with open(output, "r", encoding="utf-8") as handle:
            return handle.read()


def parse_netlist_xml(xml_text: str) -> dict:
    """Parse KiCad's netlist XML into components, nets and libparts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NetlistUnavailable(f"netlist XML was malformed: {exc}") from exc

    design = root.find("design")
    sheets = []
    if design is not None:
        for sheet in design.findall("sheet"):
            block = sheet.find("title_block")
            sheets.append({
                "number": sheet.get("number"),
                "name": sheet.get("name"),
                "title": (block.findtext("title") if block is not None else None) or "",
            })

    components = []
    comps_node = root.find("components")
    if comps_node is not None:
        for comp in comps_node.findall("comp"):
            libsource = comp.find("libsource")
            components.append({
                "reference": comp.get("ref") or "",
                "value": (comp.findtext("value") or "").strip(),
                "footprint": (comp.findtext("footprint") or "").strip(),
                "datasheet": (comp.findtext("datasheet") or "").strip(),
                "library": libsource.get("lib") if libsource is not None else "",
                "part": libsource.get("part") if libsource is not None else "",
                "description": libsource.get("description") if libsource is not None else "",
                "sheet": (comp.find("sheetpath").get("names")
                          if comp.find("sheetpath") is not None else "/"),
            })

    nets = []
    nets_node = root.find("nets")
    if nets_node is not None:
        for net in nets_node.findall("net"):
            nodes = [{
                "reference": node.get("ref") or "",
                "pin": node.get("pin") or "",
                "function": node.get("pinfunction") or "",
                "type": node.get("pintype") or "",
            } for node in net.findall("node")]
            nets.append({
                "code": net.get("code"),
                "name": net.get("name") or "",
                "nodes": nodes,
                "node_count": len(nodes),
            })

    libparts = []
    libparts_node = root.find("libparts")
    if libparts_node is not None:
        for part in libparts_node.findall("libpart"):
            pins = []
            pins_node = part.find("pins")
            if pins_node is not None:
                pins = [{"num": p.get("num"), "name": p.get("name"), "type": p.get("type")}
                        for p in pins_node.findall("pin")]
            libparts.append({
                "lib": part.get("lib"), "part": part.get("part"),
                "description": part.findtext("description") or "",
                "pins": pins,
            })

    return {"components": components, "nets": nets, "libparts": libparts, "sheets": sheets}


def load_netlist(schematic_path: str, cli_path: str | None = None) -> dict:
    return parse_netlist_xml(export_netlist_xml(schematic_path, cli_path))
