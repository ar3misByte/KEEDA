r"""Generate the KiCad Live demo board (sample_project/demo_board.kicad_pcb).

Must be run with KiCad's bundled Python, which provides `pcbnew`:

    "C:\Program Files\KiCad.0in\python.exe" tools/make_sample_board.py

Creates a small, deliberately simple board holding the components the demo
script refers to by name: R1, R2, C1, C2, U1, J1.
"""
import os
import sys

import pcbnew

# (reference, value, library, footprint, x_mm, y_mm)
PARTS = [
    ("R1", "10k",   "Resistor_SMD",              "R_0805_2012Metric",                    60.0,  50.0),
    ("R2", "4k7",   "Resistor_SMD",              "R_0805_2012Metric",                    60.0,  60.0),
    ("C1", "100nF", "Capacitor_SMD",             "C_0805_2012Metric",                    80.0,  50.0),
    ("C2", "10uF",  "Capacitor_SMD",             "C_0805_2012Metric",                    80.0,  60.0),
    ("U1", "NE555", "Package_DIP",               "DIP-8_W7.62mm",                       100.0,  55.0),
    ("J1", "CONN",  "Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical",     125.0,  55.0),
]

FP_ROOT = os.path.join(
    os.environ.get("KICAD_FOOTPRINT_DIR")
    or r"C:\Program Files\KiCad\10.0\share\kicad\footprints"
)

BOARD_EDGE = (50.0, 40.0, 140.0, 75.0)  # x1, y1, x2, y2 in mm


def mm(value):
    return pcbnew.FromMM(value)


def main():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sample_project")
    out_dir = os.path.normpath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "demo_board.kicad_pcb")

    board = pcbnew.BOARD()

    for ref, value, lib, fp_name, x, y in PARTS:
        lib_path = os.path.join(FP_ROOT, lib + ".pretty")
        fp = pcbnew.FootprintLoad(lib_path, fp_name)
        if fp is None:
            print("[FAIL] could not load %s:%s from %s" % (lib, fp_name, lib_path))
            return 1
        fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
        fp.SetReference(ref)
        fp.SetValue(value)
        board.Add(fp)
        print("[ OK ] placed %-3s %-6s at (%.1f, %.1f)" % (ref, value, x, y))

    # Board outline on Edge.Cuts so the board has a defined extent.
    x1, y1, x2, y2 = BOARD_EDGE
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    for i in range(4):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pcbnew.VECTOR2I(mm(corners[i][0]), mm(corners[i][1])))
        seg.SetEnd(pcbnew.VECTOR2I(mm(corners[(i + 1) % 4][0]), mm(corners[(i + 1) % 4][1])))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(mm(0.1))
        board.Add(seg)
    print("[ OK ] board outline added")

    board.Save(out_path)
    print("[ OK ] saved %s" % out_path)

    # Write a .kicad_pro so KiCad opens it as a project.
    pro_path = os.path.join(out_dir, "demo_board.kicad_pro")
    if not os.path.exists(pro_path):
        with open(pro_path, "w", encoding="utf-8") as fh:
            fh.write('{\n  "board": {},\n  "meta": {"filename": "demo_board.kicad_pro", "version": 1}\n}\n')
        print("[ OK ] wrote %s" % pro_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
