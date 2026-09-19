r"""Milestone 1 - verify the pcbnew file API round-trips a modification.

Run with KiCad's bundled Python (the one that owns the `pcbnew` module):

    "C:\Program Files\KiCad.0in\python.exe" tests/manual/test_pcb_api.py

This proves, on THIS machine, that we can load a board, enumerate footprints,
change a position, save, reload, and see the change survive. Everything the
sync layer does to board files rests on that being true, so this test runs
before any of it.
"""
import os
import shutil
import sys
import tempfile

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append(bool(ok))
    tag = "[PASS]" if ok else "[FAIL]"
    print("%s %s%s" % (tag, label, (" - " + detail) if detail else ""))
    return ok


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.normpath(os.path.join(here, "..", "..", "sample_project", "demo_board.kicad_pcb"))

    try:
        import pcbnew
    except ImportError as exc:
        print("[FAIL] `import pcbnew` failed: %s" % exc)
        print("       Run this with KiCad's bundled Python, not a virtualenv.")
        return 1

    check(True, "pcbnew imported", "build %s" % pcbnew.GetBuildVersion())

    if not check(os.path.exists(src), "Sample board exists", src):
        print("       Run: tools/make_sample_board.py first.")
        return 1

    # Work on a scratch copy so the repo's sample board is never mutated.
    tmpdir = tempfile.mkdtemp(prefix="kicadlive_t1_")
    work = os.path.join(tmpdir, "demo_board.kicad_pcb")
    shutil.copyfile(src, work)

    try:
        board = pcbnew.LoadBoard(work)
        check(board is not None, "Board loaded")

        footprints = list(board.GetFootprints())
        check(len(footprints) > 0, "Found %d footprints" % len(footprints))
        for fp in footprints:
            pos = fp.GetPosition()
            print("       %-4s %-8s (%.3f, %.3f) mm  rot=%.1f  layer=%s"
                  % (fp.GetReference(), fp.GetValue(),
                     pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y),
                     fp.GetOrientationDegrees(), board.GetLayerName(fp.GetLayer())))

        target = board.FindFootprintByReference("R1")
        if not check(target is not None, "R1 found"):
            return 1

        # UUIDs are how the sync layer names objects; confirm they are stable.
        uuid_before = target.m_Uuid.AsString()
        check(bool(uuid_before), "R1 has a UUID", uuid_before)

        old = target.GetPosition()
        new_x = old.x + pcbnew.FromMM(5.0)
        target.SetPosition(pcbnew.VECTOR2I(new_x, old.y))
        check(target.GetPosition().x == new_x, "Position changed",
              "%.3f -> %.3f mm" % (pcbnew.ToMM(old.x), pcbnew.ToMM(new_x)))

        check(board.Save(work), "Board saved")

        reloaded = pcbnew.LoadBoard(work)
        check(reloaded is not None, "Board reloaded")

        r1 = reloaded.FindFootprintByReference("R1")
        check(r1 is not None and r1.GetPosition().x == new_x,
              "Modification persisted",
              "R1.x = %.3f mm" % pcbnew.ToMM(r1.GetPosition().x) if r1 else "R1 missing")

        check(r1 is not None and r1.m_Uuid.AsString() == uuid_before,
              "UUID stable across save/reload", uuid_before)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    failed = RESULTS.count(False)
    if failed:
        print("RESULT: %d/%d checks FAILED" % (failed, len(RESULTS)))
        return 1
    print("RESULT: all %d checks PASSED" % len(RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
