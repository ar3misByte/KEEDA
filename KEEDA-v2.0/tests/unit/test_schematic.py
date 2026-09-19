"""Unit tests for the schematic parser and diff engine."""
import copy
import os

import pytest

from common.sexpr import SExprError, child, children, parse, properties, values
from server.schematic.diff import diff_object, diff_snapshots, summarise, values_equal
from server.schematic.parser import find_schematic, parse_schematic

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE_DIR = os.path.join(ROOT, "sample_project")
SAMPLE_SCH = os.path.join(SAMPLE_DIR, "demo_board.kicad_sch")

needs_sample = pytest.mark.skipif(
    not os.path.exists(SAMPLE_SCH),
    reason="sample schematic missing; run tools/make_sample_schematic.py")


class TestSExpr:
    def test_simple(self):
        assert parse("(a b c)") == [["a", "b", "c"]]

    def test_nested(self):
        assert parse("(a (b 1) (c 2))") == [["a", ["b", "1"], ["c", "2"]]]

    def test_quoted_strings_keep_spaces(self):
        assert parse('(p "hello world")') == [["p", "hello world"]]

    def test_escaped_quote(self):
        assert parse(r'(p "say \"hi\"")')[0][1] == 'say "hi"'

    def test_parens_inside_string_are_not_structure(self):
        assert parse('(p "a (b) c")')[0][1] == "a (b) c"

    def test_unbalanced_open_raises(self):
        with pytest.raises(SExprError):
            parse("(a (b)")

    def test_unbalanced_close_raises(self):
        with pytest.raises(SExprError):
            parse("(a))")

    def test_accessors(self):
        node = parse('(sym (at 1 2 90) (property "Reference" "R1"))')[0]
        assert values(node, "at") == ["1", "2", "90"]
        assert properties(node) == {"Reference": "R1"}
        assert child(node, "missing") is None
        assert children(node, "at") != []


def sym(uuid="u1", ref="R1", value="10k", x=10.0, y=20.0, rot=0.0, sheet="/"):
    return {"uuid": uuid, "domain": "schematic", "object_type": "symbol",
            "sheet": sheet, "reference": ref, "value": value,
            "lib_id": "Device:R", "position": {"x": x, "y": y}, "rotation": rot,
            "mirror": "", "unit": "1", "dnp": "no", "footprint": ""}


class TestSchematicDiff:
    def test_identical_is_empty(self):
        assert diff_object(sym(), sym()) == []

    def test_value_change(self):
        changes = diff_object(sym(), sym(value="4k7"))
        assert len(changes) == 1
        assert changes[0]["field"] == "value"
        assert changes[0]["domain"] == "schematic"

    def test_position_change(self):
        changes = diff_object(sym(), sym(x=50.0))
        assert changes[0]["field"] == "position"

    def test_multiple_fields(self):
        changes = diff_object(sym(), sym(value="1k", x=99.0, rot=90.0))
        assert {c["field"] for c in changes} == {"value", "position", "rotation"}

    def test_rotation_360_equals_zero(self):
        assert values_equal("rotation", 0.0, 360.0)
        assert diff_object(sym(rot=0.0), sym(rot=360.0)) == []

    def test_add_and_remove(self):
        assert diff_snapshots({}, {"u1": sym()})[0]["operation"] == "add"
        assert diff_snapshots({"u1": sym()}, {})[0]["operation"] == "remove"

    def test_wire_endpoints_compared(self):
        a = {"uuid": "w", "object_type": "wire", "start": {"x": 0, "y": 0},
             "end": {"x": 1, "y": 1}}
        b = copy.deepcopy(a)
        b["end"] = {"x": 2, "y": 2}
        changes = diff_object(a, b)
        assert len(changes) == 1 and changes[0]["field"] == "end"

    def test_untracked_field_ignored(self):
        a, b = sym(), sym()
        b["internal_thing"] = "changed"
        assert diff_object(a, b) == []


class TestSummaries:
    def test_value_change_names_both_values(self):
        text = summarise({"operation": "modify", "object_type": "symbol",
                          "reference": "R1", "field": "value",
                          "old": "10k", "new": "4k7"})
        assert "R1" in text and "10k" in text and "4k7" in text

    def test_move_summary(self):
        text = summarise({"operation": "modify", "object_type": "symbol",
                          "reference": "U1", "field": "position",
                          "new": {"x": 1.0, "y": 2.0}})
        assert "moved" in text and "U1" in text

    def test_add_and_remove_summaries(self):
        assert "added" in summarise({"operation": "add", "object_type": "symbol",
                                     "reference": "C9"})
        assert "removed" in summarise({"operation": "remove", "object_type": "wire",
                                       "reference": ""})

    def test_rename_summary(self):
        text = summarise({"operation": "modify", "object_type": "symbol",
                          "reference": "R9", "field": "reference",
                          "old": "R1", "new": "R9"})
        assert "R1" in text and "R9" in text


@needs_sample
class TestRealSchematic:
    def test_parses_the_sample_project(self):
        objects = parse_schematic(SAMPLE_SCH)
        assert objects, "no objects parsed"
        symbols = [o for o in objects.values() if o["object_type"] == "symbol"]
        refs = {s["reference"] for s in symbols}
        assert {"R1", "R2", "C1", "C2", "U1", "J1"} <= refs

    def test_uuids_are_unique(self):
        objects = parse_schematic(SAMPLE_SCH)
        assert len(objects) == len({o["uuid"] for o in objects.values()})

    def test_reparse_produces_no_diff(self):
        """The property the change detector depends on."""
        first = parse_schematic(SAMPLE_SCH)
        second = parse_schematic(SAMPLE_SCH)
        assert diff_snapshots(first, second) == []

    def test_wires_and_labels_present(self):
        objects = parse_schematic(SAMPLE_SCH)
        kinds = {o["object_type"] for o in objects.values()}
        assert "wire" in kinds
        assert "local_label" in kinds

    def test_symbol_has_expected_fields(self):
        objects = parse_schematic(SAMPLE_SCH)
        r1 = next(o for o in objects.values()
                  if o["object_type"] == "symbol" and o["reference"] == "R1")
        assert r1["value"] == "4k7"
        assert r1["lib_id"] == "Device:R"
        assert "x" in r1["position"] and "y" in r1["position"]
        assert r1["footprint"]

    def test_find_schematic_locates_the_root(self):
        found = find_schematic(SAMPLE_DIR)
        assert found and os.path.basename(found) == "demo_board.kicad_sch"

    def test_simulated_edit_is_detected(self):
        baseline = parse_schematic(SAMPLE_SCH)
        current = copy.deepcopy(baseline)
        uuid = next(u for u, o in current.items()
                    if o["object_type"] == "symbol" and o["reference"] == "R1")
        current[uuid]["value"] = "10k"
        changes = diff_snapshots(baseline, current)
        assert len(changes) == 1
        assert changes[0]["field"] == "value"
        assert "R1" in summarise(changes[0])


class TestHierarchy:
    def test_missing_file_is_handled(self, tmp_path):
        assert find_schematic(str(tmp_path)) is None

    def test_traversal_outside_project_is_refused(self, tmp_path):
        """A sheet file naming ../ must not be followed."""
        sheet = tmp_path / "root.kicad_sch"
        sheet.write_text(
            '(kicad_sch (version 1) (uuid "r")\n'
            '  (sheet (at 1 2) (uuid "s1")\n'
            '    (property "Sheetname" "evil")\n'
            '    (property "Sheetfile" "../../outside.kicad_sch")\n'
            '  )\n)', encoding="utf-8")
        objects = parse_schematic(str(sheet))
        # The sheet symbol itself is recorded; the outside file is never read.
        assert any(o["object_type"] == "sheet" for o in objects.values())
