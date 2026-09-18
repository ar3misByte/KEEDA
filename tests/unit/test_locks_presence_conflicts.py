"""Unit tests for locks, presence, conflict detection and versioning."""
import time

import pytest

from common.protocol import (
    ValidationError, validate_change, validate_client_id, validate_project_id,
    validate_selection, validate_user_name, validate_uuid,
)
from server.conflict_detector import detect
from server.lock_manager import LockManager
from server.presence_manager import PresenceManager
from server.project_manager import ProjectManager
from server.version_manager import VersionManager

UID = "33c18730-0af9-4ad4-972d-782bf1b87199"


# ---------------------------------------------------------------- validation

class TestValidation:
    def test_good_ids_pass(self):
        assert validate_client_id("a3f1c0d2") == "a3f1c0d2"
        assert validate_project_id("demo_board") == "demo_board"
        assert validate_uuid(UID) == UID

    @pytest.mark.parametrize("bad", ["", "a" * 65, "has space", "semi;colon", None, 42])
    def test_bad_client_ids_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_client_id(bad)

    @pytest.mark.parametrize("bad", ["../etc/passwd", "..\\windows", "a/b", "a\\b", "..", "."])
    def test_path_traversal_project_ids_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_project_id(bad)

    def test_user_name_trimmed_and_bounded(self):
        assert validate_user_name("  Aditya  ") == "Aditya"
        with pytest.raises(ValidationError):
            validate_user_name("   ")
        with pytest.raises(ValidationError):
            validate_user_name("x" * 49)

    def test_selection_is_capped_and_cleaned(self):
        assert validate_selection(None) == []
        assert validate_selection(["R1", "", "  C1 ", 5]) == ["R1", "C1"]
        assert len(validate_selection([f"R{i}" for i in range(100)])) == 32

    def test_change_rejects_unsupported_field(self):
        with pytest.raises(ValidationError):
            validate_change({"operation": "modify", "uuid": UID, "field": "net_secrets"})

    def test_change_rejects_negative_base_version(self):
        with pytest.raises(ValidationError):
            validate_change({"operation": "modify", "uuid": UID,
                             "field": "position", "base_version": -1})

    def test_valid_change_passes(self):
        out = validate_change({"operation": "modify", "uuid": UID, "reference": "R1",
                               "field": "position", "base_version": 3,
                               "old": {"x": 1.0, "y": 2.0}, "new": {"x": 5.0, "y": 2.0}})
        assert out["field"] == "position" and out["base_version"] == 3


# --------------------------------------------------------------------- locks

class TestLockManager:
    def test_first_request_is_granted(self):
        locks = LockManager()
        granted, lock = locks.request("p", UID, "R1", "A", "Aditya")
        assert granted and lock.owner == "A"

    def test_second_client_is_denied(self):
        locks = LockManager()
        locks.request("p", UID, "R1", "A", "Aditya")
        granted, lock = locks.request("p", UID, "R1", "B", "Rahul")
        assert not granted
        assert lock.owner == "A" and lock.owner_name == "Aditya"

    def test_owner_can_refresh_own_lock(self):
        locks = LockManager()
        locks.request("p", UID, "R1", "A", "Aditya")
        granted, _ = locks.request("p", UID, "R1", "A", "Aditya")
        assert granted

    def test_release_allows_another_client(self):
        locks = LockManager()
        locks.request("p", UID, "R1", "A", "Aditya")
        assert locks.release("p", UID, "A")
        granted, lock = locks.request("p", UID, "R1", "B", "Rahul")
        assert granted and lock.owner == "B"

    def test_release_by_non_owner_is_a_noop(self):
        locks = LockManager()
        locks.request("p", UID, "R1", "A", "Aditya")
        assert locks.release("p", UID, "B") is False
        assert locks.owner_of("p", UID).owner == "A"

    def test_disconnect_releases_every_lock(self):
        locks = LockManager()
        locks.request("p", UID, "R1", "A", "Aditya")
        locks.request("p", "other-uuid", "C1", "A", "Aditya")
        assert len(locks.release_all_for_client("A")) == 2
        assert locks.snapshot("p") == []

    def test_lock_expires_after_ttl(self):
        locks = LockManager(ttl=0.05)
        locks.request("p", UID, "R1", "A", "Aditya")
        time.sleep(0.08)
        assert locks.expire_stale() == [UID]
        granted, lock = locks.request("p", UID, "R1", "B", "Rahul")
        assert granted and lock.owner == "B"

    def test_is_blocked_for(self):
        locks = LockManager()
        locks.request("p", UID, "R1", "A", "Aditya")
        assert locks.is_blocked_for("p", UID, "B") is not None
        assert locks.is_blocked_for("p", UID, "A") is None

    def test_projects_have_independent_lock_tables(self):
        locks = LockManager()
        locks.request("p1", UID, "R1", "A", "Aditya")
        granted, _ = locks.request("p2", UID, "R1", "B", "Rahul")
        assert granted


# ------------------------------------------------------------------ presence

class TestPresenceManager:
    def test_register_and_count(self):
        presence = PresenceManager()
        presence.register("A", "Aditya", "p")
        presence.register("B", "Rahul", "p")
        assert presence.online_count("p") == 2

    def test_disconnect_reduces_online_count(self):
        presence = PresenceManager()
        presence.register("A", "Aditya", "p")
        presence.register("B", "Rahul", "p")
        presence.mark_offline("B")
        assert presence.online_count("p") == 1

    def test_status_text_reflects_selection(self):
        presence = PresenceManager()
        presence.register("A", "Aditya", "p")
        presence.update("A", activity="editing", selection=["R1"], kicad_connected=True)
        assert presence.get("A").describe() == "Editing R1"

    def test_unknown_activity_is_coerced(self):
        presence = PresenceManager()
        presence.register("A", "Aditya", "p")
        presence.update("A", activity="doing-something-weird", kicad_connected=True)
        assert presence.get("A").activity == "idle"

    def test_reconnect_reuses_record(self):
        presence = PresenceManager()
        presence.register("A", "Aditya", "p")
        presence.mark_offline("A")
        presence.register("A", "Aditya", "p")
        assert presence.get("A").online
        assert presence.online_count("p") == 1

    def test_stale_client_detected(self):
        presence = PresenceManager()
        presence.register("A", "Aditya", "p")
        presence.get("A").last_seen -= 60
        assert presence.stale_clients() == ["A"]


# ----------------------------------------------------------------- conflicts

def record(version=7, field_version=7, x=40.0, y=30.0, writer="Aditya"):
    return {"uuid": UID, "reference": "R1", "position": {"x": x, "y": y},
            "version": version, "field_versions": {"position": field_version},
            "field_writers": {"position": writer}, "last_writer": writer}


def change(base_version=7, new_x=45.0, old_x=40.0):
    return {"operation": "modify", "uuid": UID, "reference": "R1", "field": "position",
            "base_version": base_version,
            "old": {"x": old_x, "y": 30.0}, "new": {"x": new_x, "y": 30.0}}


class TestConflictDetector:
    def test_up_to_date_change_is_accepted(self):
        assert detect(change(base_version=7), record(field_version=7)) is None

    def test_unknown_object_is_never_a_conflict(self):
        assert detect(change(), None) is None

    def test_stale_change_to_moved_object_conflicts(self):
        conflict = detect(change(base_version=7, new_x=40.0, old_x=40.0),
                          record(field_version=8, x=45.0))
        assert conflict is not None
        assert conflict.field == "position"
        assert conflict.your_base_version == 7 and conflict.current_version == 8
        assert conflict.conflicting_user == "Aditya"

    def test_idempotent_resend_is_not_a_conflict(self):
        # Server already holds exactly what the client is asking for.
        assert detect(change(base_version=5, new_x=45.0), record(field_version=9, x=45.0)) is None

    def test_stale_version_but_unchanged_value_is_not_a_conflict(self):
        # Another field moved the object's version on; ours is untouched.
        assert detect(change(base_version=5, old_x=40.0, new_x=50.0),
                      record(field_version=5, x=40.0)) is None

    def test_different_fields_do_not_conflict(self):
        rotation_change = {"operation": "modify", "uuid": UID, "reference": "R1",
                           "field": "rotation", "base_version": 5, "old": 0.0, "new": 90.0}
        assert detect(rotation_change, record(field_version=9, x=45.0)) is None

    def test_remove_and_add_are_not_conflicts(self):
        assert detect({"operation": "remove", "uuid": UID}, record()) is None
        assert detect({"operation": "add", "uuid": UID, "state": {}}, record()) is None


# --------------------------------------------------------- project + version

class TestProjectManager:
    def make(self, tmp_path):
        return ProjectManager(VersionManager(str(tmp_path)))

    def test_accepted_change_bumps_version(self, tmp_path):
        projects = self.make(tmp_path)
        result = projects.submit("p", "A", "Aditya", "c1", [validate_change(change(0))])
        assert len(result["accepted"]) == 1
        assert projects.get("p").version == 1

    def test_duplicate_change_id_is_idempotent(self, tmp_path):
        projects = self.make(tmp_path)
        projects.submit("p", "A", "Aditya", "c1", [validate_change(change(0))])
        version_after_first = projects.get("p").version
        result = projects.submit("p", "A", "Aditya", "c1", [validate_change(change(0))])
        assert result["duplicate"] is True
        assert projects.get("p").version == version_after_first

    def test_locked_object_is_rejected_not_conflicted(self, tmp_path):
        projects = self.make(tmp_path)
        locks = LockManager()
        locks.request("p", UID, "R1", "A", "Aditya")
        result = projects.submit(
            "p", "B", "Rahul", "c2", [validate_change(change(0))],
            blocked_checker=lambda u: locks.is_blocked_for("p", u, "B"))
        assert result["accepted"] == []
        assert result["rejected"][0]["reason"] == "locked"
        assert result["rejected"][0]["owner_name"] == "Aditya"

    def test_second_client_stale_edit_conflicts(self, tmp_path):
        projects = self.make(tmp_path)
        projects.submit("p", "A", "Aditya", "c1", [validate_change(change(0, new_x=45.0))])
        result = projects.submit("p", "B", "Rahul", "c2",
                                 [validate_change(change(0, new_x=40.0, old_x=40.0))])
        assert result["accepted"] == []
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["conflicting_user"] == "Aditya"

    def test_force_apply_wins(self, tmp_path):
        projects = self.make(tmp_path)
        projects.submit("p", "A", "Aditya", "c1", [validate_change(change(0, new_x=45.0))])
        result = projects.force_apply("p", "Rahul", UID, "position", {"x": 99.0, "y": 30.0}, "R1")
        assert projects.get("p").objects[UID]["position"] == {"x": 99.0, "y": 30.0}
        assert result["version"] == projects.get("p").version

    def test_history_is_recorded_and_persisted(self, tmp_path):
        versions = VersionManager(str(tmp_path))
        projects = ProjectManager(versions)
        projects.submit("p", "A", "Aditya", "c1", [validate_change(change(0))])
        entries = versions.recent("p")
        assert len(entries) == 1 and entries[0]["user_name"] == "Aditya"
        # A fresh manager over the same dir must see the persisted entry.
        assert VersionManager(str(tmp_path)).load("p") == 1

    def test_version_continues_after_restart(self, tmp_path):
        versions = VersionManager(str(tmp_path))
        projects = ProjectManager(versions)
        projects.submit("p", "A", "Aditya", "c1", [validate_change(change(0))])
        first = projects.get("p").version
        # Simulate a server restart with the same data directory.
        restarted = ProjectManager(VersionManager(str(tmp_path)))
        assert restarted.get("p").version >= first

    def test_history_path_rejects_traversal(self, tmp_path):
        versions = VersionManager(str(tmp_path))
        with pytest.raises(ValidationError):
            versions.load("../escape")
