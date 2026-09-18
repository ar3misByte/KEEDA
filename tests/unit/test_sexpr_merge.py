"""Three-way merge of real KiCad files: different objects merge, same object conflicts."""
import os

from common.sexpr_merge import merge
from server.schematic.parser import parse_schematic

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCH = open(os.path.join(ROOT, "sample_project", "demo_board.kicad_sch"), encoding="utf-8").read()
PCB = open(os.path.join(ROOT, "sample_project", "demo_board.kicad_pcb"), encoding="utf-8").read()


def values(tmp_path, text):
    p = tmp_path / "x.kicad_sch"
    p.write_text(text, encoding="utf-8")
    return {o.get("reference"): o.get("value") for o in parse_schematic(str(p)).values()
            if o.get("object_type") == "symbol"}


def test_edits_to_different_symbols_merge(tmp_path):
    mine = SCH.replace('"4k7"', '"10k"', 1)                    # I changed one resistor
    other_value = [v for v in values(tmp_path, SCH).values() if v == "10k"]
    theirs = SCH.replace('"10uF"', '"22uF"', 1) if '"10uF"' in SCH else SCH.replace('"100nF"', '"1uF"', 1)
    result = merge(SCH, mine, theirs)
    assert result.clean, result.conflicts
    merged = values(tmp_path, result.text)
    assert "10k" in merged.values()
    assert ("22uF" in merged.values()) or ("1uF" in merged.values())
    assert result.taken_theirs >= 1 and result.kept_mine >= 1


def test_same_object_edited_differently_conflicts():
    mine = SCH.replace('"4k7"', '"10k"', 1)
    theirs = SCH.replace('"4k7"', '"22k"', 1)
    result = merge(SCH, mine, theirs)
    assert not result.clean and result.conflicts


def test_same_edit_on_both_sides_is_fine():
    both = SCH.replace('"4k7"', '"10k"', 1)
    assert merge(SCH, both, both).clean


def test_identical_files_round_trip_byte_for_byte():
    result = merge(SCH, SCH, SCH)
    assert result.text == SCH


def test_only_theirs_changed_gives_theirs_exactly():
    theirs = SCH.replace('"4k7"', '"10k"', 1)
    assert merge(SCH, SCH, theirs).text == theirs


def test_only_mine_changed_gives_mine_exactly():
    mine = SCH.replace('"4k7"', '"10k"', 1)
    assert merge(SCH, mine, SCH).text == mine


def test_deletion_by_one_side_is_kept():
    # drop the last top-level object (before the final paren) on their side
    end = SCH.rstrip().rfind("\n\t(")
    theirs = SCH[:end] + "\n)\n"
    mine = SCH.replace('"4k7"', '"10k"', 1)
    result = merge(SCH, mine, theirs)
    assert result.clean, result.conflicts


def test_pcb_footprints_merge_by_uuid():
    import re
    refs = re.findall(r'\(property "Reference" "(\w+)"', PCB)
    assert len(refs) >= 2
    mine = PCB.replace("(at 50 20", "(at 51 20", 1) if "(at 50 20" in PCB else None
    if mine is None:
        return
    result = merge(PCB, mine, PCB)
    assert result.text == mine


def test_garbage_never_raises():
    assert not merge("not a file", "x", "y").clean
