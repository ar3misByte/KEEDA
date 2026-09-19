"""Unit tests for the structured diff engine."""
from common.diff_engine import (
    apply_change, diff_object, diff_snapshots, mm_to_nm, nm_to_mm,
    normalise_rotation, summarise, values_equal,
)


def fp(uuid="u1", ref="R1", x=10.0, y=20.0, rot=0.0, layer="F.Cu", value="10k"):
    return {"uuid": uuid, "reference": ref, "object_type": "footprint",
            "position": {"x": x, "y": y}, "rotation": rot, "layer": layer, "value": value}


class TestUnits:
    def test_nm_mm_roundtrip(self):
        assert nm_to_mm(60_000_000) == 60.0
        assert mm_to_nm(60.0) == 60_000_000

    def test_sub_micron_noise_is_rounded_away(self):
        # 1 nm of jitter must not register as a change.
        assert nm_to_mm(60_000_000) == nm_to_mm(60_000_001)

    def test_rotation_folds_to_360(self):
        assert normalise_rotation(360.0) == 0.0
        assert normalise_rotation(-90.0) == 270.0
        assert normalise_rotation(450.0) == 90.0


class TestEquality:
    def test_positions_compare_by_value(self):
        assert values_equal("position", {"x": 1.0, "y": 2.0}, {"x": 1.0, "y": 2.0})
        assert not values_equal("position", {"x": 1.0, "y": 2.0}, {"x": 1.5, "y": 2.0})

    def test_position_noise_below_resolution_is_equal(self):
        assert values_equal("position", {"x": 1.0000001, "y": 2.0}, {"x": 1.0, "y": 2.0})

    def test_rotation_0_and_360_are_equal(self):
        assert values_equal("rotation", 0.0, 360.0)


class TestDiffObject:
    def test_no_change_yields_nothing(self):
        assert diff_object(fp(), fp()) == []

    def test_position_change_detected(self):
        changes = diff_object(fp(), fp(x=15.0))
        assert len(changes) == 1
        assert changes[0]["field"] == "position"
        assert changes[0]["old"] == {"x": 10.0, "y": 20.0}
        assert changes[0]["new"] == {"x": 15.0, "y": 20.0}

    def test_multiple_fields_yield_multiple_changes(self):
        changes = diff_object(fp(), fp(x=15.0, rot=90.0, layer="B.Cu"))
        assert {c["field"] for c in changes} == {"position", "rotation", "layer"}

    def test_unsynced_fields_are_ignored(self):
        a, b = fp(), fp()
        b["some_internal_field"] = "changed"
        assert diff_object(a, b) == []


class TestDiffSnapshots:
    def test_add_detected(self):
        changes = diff_snapshots({}, {"u1": fp()})
        assert len(changes) == 1 and changes[0]["operation"] == "add"

    def test_remove_detected(self):
        changes = diff_snapshots({"u1": fp()}, {})
        assert len(changes) == 1 and changes[0]["operation"] == "remove"

    def test_modify_detected(self):
        changes = diff_snapshots({"u1": fp()}, {"u1": fp(x=99.0)})
        assert len(changes) == 1 and changes[0]["operation"] == "modify"

    def test_identical_snapshots_produce_nothing(self):
        snap = {"u1": fp(), "u2": fp(uuid="u2", ref="C1")}
        assert diff_snapshots(snap, dict(snap)) == []

    def test_mixed_operations(self):
        before = {"u1": fp(), "u2": fp(uuid="u2", ref="C1")}
        after = {"u1": fp(x=50.0), "u3": fp(uuid="u3", ref="U1")}
        ops = sorted(c["operation"] for c in diff_snapshots(before, after))
        assert ops == ["add", "modify", "remove"]


class TestApplyChange:
    def test_apply_modify_then_diff_is_empty(self):
        """The property the echo-suppression rule depends on."""
        baseline = {"u1": fp()}
        current = {"u1": fp(x=42.0)}
        change = diff_snapshots(baseline, current)[0]
        apply_change(baseline, change)
        assert diff_snapshots(baseline, current) == []

    def test_apply_add(self):
        snap = {}
        apply_change(snap, {"operation": "add", "uuid": "u9", "state": fp(uuid="u9")})
        assert "u9" in snap

    def test_apply_remove(self):
        snap = {"u1": fp()}
        apply_change(snap, {"operation": "remove", "uuid": "u1"})
        assert snap == {}

    def test_unknown_field_is_ignored(self):
        snap = {"u1": fp()}
        apply_change(snap, {"operation": "modify", "uuid": "u1",
                            "field": "not_a_field", "new": "x"})
        assert "not_a_field" not in snap["u1"]


class TestSummarise:
    def test_move_summary_names_the_part(self):
        assert "R1" in summarise({"operation": "modify", "reference": "R1",
                                  "field": "position", "new": {"x": 1.0, "y": 2.0}})

    def test_rotate_summary(self):
        text = summarise({"operation": "modify", "reference": "U1",
                          "field": "rotation", "new": 90.0})
        assert "U1" in text and "90" in text
