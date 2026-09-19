"""Generate sample_project/demo_board.kicad_sch.

A deliberately small I2C sensor circuit whose references match the demo board:
R1, R2, C1, C2, U1, J1. It exists so the schematic collaboration features and
the hardware-summary analyzer have something real to work on out of the box.

The circuit is chosen to exercise the analyzer honestly:
  * SDA / SCL nets and pin functions  -> I2C should be DETECTED
  * +3V3 and GND nets                 -> power rails should be reported
  * nothing resembling USB            -> USB must NOT be claimed

Written as KiCad S-expressions and validated by exporting a netlist with
kicad-cli, which fails loudly if the file is malformed:

    python tools/make_sample_schematic.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import uuid as uuidlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.normpath(os.path.join(HERE, "..", "sample_project"))
OUT_PATH = os.path.join(OUT_DIR, "demo_board.kicad_sch")

# Deterministic UUIDs: regenerating the file must not change object identity,
# because identity is what synchronisation and comments key on.
NS = uuidlib.UUID("6f1b3d52-9a44-4c1f-9d2e-0b7c5a8e4f10")


def uid(name: str) -> str:
    return str(uuidlib.uuid5(NS, name))


def effects(hide: bool = False) -> str:
    return ('(effects (font (size 1.27 1.27))%s)' % (" (hide yes)" if hide else ""))


def pin_def(number: str, name: str, ptype: str, x: float, y: float, angle: int) -> str:
    return f'''\t\t\t(pin {ptype} line
\t\t\t\t(at {x} {y} {angle})
\t\t\t\t(length 2.54)
\t\t\t\t(name "{name}" {effects()})
\t\t\t\t(number "{number}" {effects()})
\t\t\t)'''


def lib_symbol(lib_id: str, ref_prefix: str, value: str, pins: list, body: str) -> str:
    pin_text = "\n".join(pins)
    return f'''\t\t(symbol "{lib_id}"
\t\t\t(pin_numbers (hide yes))
\t\t\t(pin_names (offset 0))
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "{ref_prefix}" (at 0 2.54 0) {effects()})
\t\t\t(property "Value" "{value}" (at 0 -2.54 0) {effects()})
\t\t\t(property "Footprint" "" (at 0 0 0) {effects(True)})
\t\t\t(property "Datasheet" "" (at 0 0 0) {effects(True)})
\t\t\t(symbol "{lib_id.split(':')[1]}_1_1"
{body}
{pin_text}
\t\t\t)
\t\t)'''


RECT = ('\t\t\t\t(rectangle (start -1.016 2.54) (end 1.016 -2.54)\n'
        '\t\t\t\t\t(stroke (width 0.254) (type default))\n'
        '\t\t\t\t\t(fill (type none))\n'
        '\t\t\t\t)')

IC_RECT = ('\t\t\t\t(rectangle (start -7.62 7.62) (end 7.62 -7.62)\n'
           '\t\t\t\t\t(stroke (width 0.254) (type default))\n'
           '\t\t\t\t\t(fill (type background))\n'
           '\t\t\t\t)')

CAP_BODY = ('\t\t\t\t(polyline (pts (xy -2.032 -0.762) (xy 2.032 -0.762))\n'
            '\t\t\t\t\t(stroke (width 0.508) (type default)) (fill (type none)))\n'
            '\t\t\t\t(polyline (pts (xy -2.032 0.762) (xy 2.032 0.762))\n'
            '\t\t\t\t\t(stroke (width 0.508) (type default)) (fill (type none)))')

CONN_RECT = ('\t\t\t\t(rectangle (start -1.27 5.08) (end 1.27 -5.08)\n'
             '\t\t\t\t\t(stroke (width 0.254) (type default))\n'
             '\t\t\t\t\t(fill (type none))\n'
             '\t\t\t\t)')


def library() -> str:
    resistor = lib_symbol(
        "Device:R", "R", "R",
        [pin_def("1", "~", "passive", 0, 5.08, 270),
         pin_def("2", "~", "passive", 0, -5.08, 90)],
        RECT)
    capacitor = lib_symbol(
        "Device:C", "C", "C",
        [pin_def("1", "~", "passive", 0, 3.81, 270),
         pin_def("2", "~", "passive", 0, -3.81, 90)],
        CAP_BODY)
    sensor = lib_symbol(
        "Sensor:BME280", "U", "BME280",
        [pin_def("1", "VDD", "power_in", -10.16, 5.08, 0),
         pin_def("2", "GND", "power_in", -10.16, -5.08, 0),
         pin_def("3", "SDA", "bidirectional", 10.16, 2.54, 180),
         pin_def("4", "SCL", "input", 10.16, 0, 180)],
        IC_RECT)
    connector = lib_symbol(
        "Connector:Conn_01x04", "J", "Conn_01x04",
        [pin_def("1", "Pin_1", "passive", -5.08, 3.81, 0),
         pin_def("2", "Pin_2", "passive", -5.08, 1.27, 0),
         pin_def("3", "Pin_3", "passive", -5.08, -1.27, 0),
         pin_def("4", "Pin_4", "passive", -5.08, -3.81, 0)],
        CONN_RECT)
    return "\n".join([resistor, capacitor, sensor, connector])


def symbol(lib_id: str, reference: str, value: str, footprint: str,
           x: float, y: float, rotation: int = 0) -> str:
    return f'''\t(symbol
\t\t(lib_id "{lib_id}")
\t\t(at {x} {y} {rotation})
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "{uid(reference)}")
\t\t(property "Reference" "{reference}" (at {x + 2.54} {y - 1.27} 0) {effects()})
\t\t(property "Value" "{value}" (at {x + 2.54} {y + 1.27} 0) {effects()})
\t\t(property "Footprint" "{footprint}" (at {x} {y} 0) {effects(True)})
\t\t(property "Datasheet" "" (at {x} {y} 0) {effects(True)})
\t\t(instances
\t\t\t(project "demo_board"
\t\t\t\t(path "/{uid('root')}" (reference "{reference}") (unit 1))
\t\t\t)
\t\t)
\t)'''


def wire(x1: float, y1: float, x2: float, y2: float, key: str) -> str:
    return (f'\t(wire (pts (xy {x1} {y1}) (xy {x2} {y2}))\n'
            f'\t\t(stroke (width 0) (type default))\n'
            f'\t\t(uuid "{uid("wire-" + key)}")\n\t)')


def label(text: str, x: float, y: float, rotation: int, key: str) -> str:
    return (f'\t(label "{text}" (at {x} {y} {rotation})\n'
            f'\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n'
            f'\t\t(uuid "{uid("label-" + key)}")\n\t)')


def build() -> str:
    """A small, genuinely connected I2C sensor circuit.

    Pin coordinates are derived, not guessed: a pin defined at (px, py) in
    symbol space lands at (x + px, y - py) when the symbol is placed at (x, y),
    because schematic Y grows downwards. Each pin gets a short stub wire ending
    on a net label, which is how the nets are formed.
    """
    placements = [
        ("Device:R", "R1", "4k7", "Resistor_SMD:R_0805_2012Metric", 60.0, 50.0),
        ("Device:R", "R2", "4k7", "Resistor_SMD:R_0805_2012Metric", 70.0, 50.0),
        ("Device:C", "C1", "100nF", "Capacitor_SMD:C_0805_2012Metric", 80.0, 50.0),
        ("Device:C", "C2", "10uF", "Capacitor_SMD:C_0805_2012Metric", 88.0, 50.0),
        ("Sensor:BME280", "U1", "BME280", "Package_DIP:DIP-8_W7.62mm", 105.0, 55.0),
        ("Connector:Conn_01x04", "J1", "Conn_01x04",
         "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", 130.0, 55.0),
    ]
    parts = [symbol(lib, ref, val, fp, x, y) for lib, ref, val, fp, x, y in placements]

    # (pin x, pin y, stub end x, stub end y, net label)
    connections = [
        (60.0, 44.92, 60.0, 41.92, "+3V3"),   # R1 pull-up to the rail
        (60.0, 55.08, 60.0, 58.08, "SDA"),    # R1 pulls up SDA
        (70.0, 44.92, 70.0, 41.92, "+3V3"),   # R2 pull-up to the rail
        (70.0, 55.08, 70.0, 58.08, "SCL"),    # R2 pulls up SCL
        (80.0, 46.19, 80.0, 41.92, "+3V3"),   # C1 decoupling
        (80.0, 53.81, 80.0, 58.08, "GND"),
        (88.0, 46.19, 88.0, 41.92, "+3V3"),   # C2 bulk
        (88.0, 53.81, 88.0, 58.08, "GND"),
        (94.84, 49.92, 90.84, 49.92, "+3V3"),  # U1 VDD
        (94.84, 60.08, 90.84, 60.08, "GND"),   # U1 GND
        (115.16, 52.46, 119.16, 52.46, "SDA"),  # U1 SDA
        (115.16, 55.0, 119.16, 55.0, "SCL"),    # U1 SCL
        (124.92, 51.19, 120.92, 51.19, "+3V3"),  # J1 break-out header
        (124.92, 53.73, 120.92, 53.73, "SDA"),
        (124.92, 56.27, 120.92, 56.27, "SCL"),
        (124.92, 58.81, 120.92, 58.81, "GND"),
    ]

    wires, labels = [], []
    for index, (x1, y1, x2, y2, net) in enumerate(connections):
        key = f"{net}-{index}"
        wires.append(wire(x1, y1, x2, y2, key))
        # The label must sit exactly on the wire end to join the net.
        labels.append(label(net, x2, y2, 0, key))

    header = f'''(kicad_sch
	(version 20260101)
	(generator "kicad-live")
	(generator_version "10.0")
	(uuid "{uid('root')}")
	(paper "A4")
	(title_block
		(title "KiCad Live demo board")
		(rev "1")
		(company "KiCad Live")
	)
	(lib_symbols
{library()}
	)'''

    footer = '''	(sheet_instances
		(path "/" (page "1"))
	)
)
'''

    return "\n".join([header] + wires + labels + parts + [footer])


def validate(path: str) -> bool:
    """Export a netlist; kicad-cli fails loudly if the file is malformed."""
    from server.project_analysis.netlist import (
        NetlistUnavailable, load_netlist,
    )
    try:
        netlist = load_netlist(path)
    except NetlistUnavailable as exc:
        print(f"[FAIL] kicad-cli could not read the schematic: {exc}")
        return False

    components = {c["reference"] for c in netlist["components"]}
    expected = {"R1", "R2", "C1", "C2", "U1", "J1"}
    print(f"[ OK ] netlist exported: {len(netlist['components'])} components, "
          f"{len(netlist['nets'])} nets")
    missing = expected - components
    if missing:
        print(f"[FAIL] missing components in netlist: {sorted(missing)}")
        return False
    print(f"[ OK ] all expected components present: {sorted(expected)}")

    names = {(n.get('name') or '').upper() for n in netlist["nets"]}
    print(f"[info] nets: {sorted(n for n in names if n)}")
    return True


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    with io.open(OUT_PATH, "w", encoding="utf-8") as handle:
        handle.write(build())
    print(f"[ OK ] wrote {OUT_PATH}")
    return 0 if validate(OUT_PATH) else 1


if __name__ == "__main__":
    sys.exit(main())
