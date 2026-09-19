"""Unit tests for persistence, the activity system and comments."""
import os

import pytest

from server.activity.event_manager import EventManager
from server.comments.manager import CommentError, CommentManager
from server.database.db import Database

PROJECT = "demo_board"


@pytest.fixture()
def db(tmp_path):
    database = Database(os.path.join(str(tmp_path), "test.db"))
    database.ensure_project(PROJECT)
    yield database
    database.close()


@pytest.fixture()
def events(db):
    return EventManager(db)


@pytest.fixture()
def comments(db, events):
    return CommentManager(db, events)


class TestDatabaseBasics:
    def test_project_is_created_once(self, db):
        db.ensure_project(PROJECT)
        db.ensure_project(PROJECT)
        assert len(db.projects()) == 1

    def test_user_roles_persist(self, db):
        db.ensure_user(PROJECT, "u1", "Aditya", "manager")
        db.ensure_user(PROJECT, "u1", "Aditya")      # a later plain reconnect
        assert db.get_user(PROJECT, "u1")["role"] == "manager", \
            "a reconnect must not silently downgrade a deliberate role"

    def test_session_lifecycle(self, db):
        session = db.start_session(PROJECT, "u1", "Aditya")
        assert db.previous_session(PROJECT, "u1") is None
        db.end_session(session)
        assert db.previous_session(PROJECT, "u1") is not None

    def test_survives_reopen(self, tmp_path):
        path = os.path.join(str(tmp_path), "persist.db")
        first = Database(path)
        first.ensure_project(PROJECT)
        first.add_event(PROJECT, "pcb", "modified", "moved R1")
        first.close()

        second = Database(path)
        assert len(second.events(PROJECT)) == 1
        second.close()


class TestEvents:
    def test_record_and_read_back(self, events):
        events.record(PROJECT, "pcb", "modified", "moved R1",
                      user_id="u1", username="Aditya", object_ref="R1")
        found = events.recent(PROJECT)
        assert len(found) == 1
        assert found[0]["description"] == "moved R1"
        assert found[0]["domain"] == "pcb"

    def test_structured_values_round_trip(self, events):
        events.record(PROJECT, "pcb", "modified", "moved R1",
                      field="position",
                      old_value={"x": 1.0, "y": 2.0}, new_value={"x": 5.0, "y": 2.0})
        entry = events.recent(PROJECT)[0]
        assert entry["old_value"] == {"x": 1.0, "y": 2.0}
        assert entry["new_value"] == {"x": 5.0, "y": 2.0}

    def test_unknown_domain_is_coerced_not_dropped(self, events):
        events.record(PROJECT, "nonsense", "modified", "something")
        assert events.recent(PROJECT)[0]["domain"] == "project"

    def test_filters(self, events):
        events.record(PROJECT, "pcb", "modified", "a", user_id="u1", username="A")
        events.record(PROJECT, "schematic", "modified", "b", user_id="u2", username="B")
        assert len(events.recent(PROJECT, domain="pcb")) == 1
        assert len(events.recent(PROJECT, user_id="u2")) == 1

    def test_record_change_maps_operations(self, events):
        events.record_change(PROJECT, "schematic", "u1", "Aditya",
                             {"operation": "add", "uuid": "x", "reference": "C9"},
                             "added symbol C9")
        assert events.recent(PROJECT)[0]["action"] == "added"


class TestWhatsNew:
    def test_first_visit_is_flagged(self, events):
        summary = events.since_last_read(PROJECT, "u1")
        assert summary["is_first_visit"] is True
        assert summary["total"] == 0

    def test_only_other_peoples_changes_count(self, events):
        events.record(PROJECT, "pcb", "modified", "mine", user_id="u1", username="A")
        events.record(PROJECT, "pcb", "modified", "theirs", user_id="u2", username="B")
        summary = events.since_last_read(PROJECT, "u1")
        assert summary["total"] == 1
        assert summary["pcb"][0]["description"] == "theirs"

    def test_marking_read_clears_it(self, events):
        events.record(PROJECT, "pcb", "modified", "theirs", user_id="u2", username="B")
        assert events.since_last_read(PROJECT, "u1")["total"] == 1
        events.mark_read(PROJECT, "u1")
        assert events.since_last_read(PROJECT, "u1")["total"] == 0

    def test_changes_are_bucketed_by_domain(self, events):
        events.record(PROJECT, "schematic", "modified", "s", user_id="u2", username="B")
        events.record(PROJECT, "pcb", "modified", "p", user_id="u2", username="B")
        events.record(PROJECT, "collab", "comment_created", "c", user_id="u2", username="B")
        summary = events.since_last_read(PROJECT, "u1")
        assert summary["counts"] == {"schematic": 1, "pcb": 1, "comments": 1}

    def test_contributors_are_counted(self, events):
        for _ in range(3):
            events.record(PROJECT, "pcb", "modified", "x", user_id="u2", username="Rahul")
        events.record(PROJECT, "pcb", "modified", "y", user_id="u3", username="Sarah")
        contributors = events.since_last_read(PROJECT, "u1")["contributors"]
        assert contributors[0] == {"username": "Rahul", "changes": 3}


class TestComments:
    def test_create_and_list(self, comments):
        comments.create(PROJECT, "schematic", "sch:R1", "Should this be 4k7?",
                        "u1", "Aditya", object_type="symbol", object_ref="R1")
        threads = comments.threads(PROJECT)
        assert len(threads) == 1
        assert threads[0]["object_ref"] == "R1"
        assert threads[0]["status"] == "open"

    def test_empty_text_refused(self, comments):
        with pytest.raises(CommentError):
            comments.create(PROJECT, "schematic", "sch:R1", "   ", "u1", "Aditya")

    def test_bad_domain_refused(self, comments):
        with pytest.raises(CommentError):
            comments.create(PROJECT, "gerber", "x", "hi", "u1", "Aditya")

    def test_object_required(self, comments):
        with pytest.raises(CommentError):
            comments.create(PROJECT, "pcb", "", "hi", "u1", "Aditya")

    def test_replies_are_threaded(self, comments):
        root = comments.create(PROJECT, "schematic", "sch:R1", "Check this",
                               "u1", "Aditya", object_ref="R1")
        comments.create(PROJECT, "schematic", "sch:R1", "Agreed", "u2", "Rahul",
                        parent_id=root["comment_id"])
        threads = comments.threads(PROJECT)
        assert len(threads) == 1
        assert threads[0]["reply_count"] == 1
        assert threads[0]["replies"][0]["author_name"] == "Rahul"

    def test_threads_stay_one_level_deep(self, comments):
        root = comments.create(PROJECT, "pcb", "u-1", "root", "u1", "A", object_ref="R1")
        reply = comments.create(PROJECT, "pcb", "u-1", "reply", "u2", "B",
                                parent_id=root["comment_id"])
        nested = comments.create(PROJECT, "pcb", "u-1", "nested", "u3", "C",
                                 parent_id=reply["comment_id"])
        assert nested["parent_id"] == root["comment_id"]
        assert comments.threads(PROJECT)[0]["reply_count"] == 2

    def test_reply_inherits_the_thread_target(self, comments):
        root = comments.create(PROJECT, "pcb", "u-1", "root", "u1", "A", object_ref="R1")
        reply = comments.create(PROJECT, "schematic", "something-else", "reply",
                                "u2", "B", parent_id=root["comment_id"])
        assert reply["object_id"] == "u-1"
        assert reply["domain"] == "pcb"

    def test_resolve_and_reopen(self, comments):
        root = comments.create(PROJECT, "pcb", "u-1", "check", "u1", "A", object_ref="R1")
        comments.set_status(PROJECT, root["comment_id"], "resolved", "u2", "B")
        assert comments.threads(PROJECT, status="resolved")
        assert comments.counts(PROJECT)["open"] == 0

        comments.set_status(PROJECT, root["comment_id"], "reopened", "u2", "B")
        assert comments.counts(PROJECT)["open"] == 1

    def test_replies_cannot_be_resolved(self, comments):
        root = comments.create(PROJECT, "pcb", "u-1", "root", "u1", "A")
        reply = comments.create(PROJECT, "pcb", "u-1", "reply", "u2", "B",
                                parent_id=root["comment_id"])
        with pytest.raises(CommentError):
            comments.set_status(PROJECT, reply["comment_id"], "resolved", "u1", "A")

    def test_filters(self, comments):
        a = comments.create(PROJECT, "pcb", "u-1", "one", "u1", "A", object_ref="R1")
        comments.create(PROJECT, "schematic", "sch:U1", "two", "u2", "B", object_ref="U1")
        comments.set_status(PROJECT, a["comment_id"], "resolved", "u1", "A")

        assert len(comments.threads(PROJECT, status="open")) == 1
        assert len(comments.threads(PROJECT, status="resolved")) == 1
        assert len(comments.threads(PROJECT, domain="schematic")) == 1

    def test_mine_filter_includes_participation(self, comments):
        root = comments.create(PROJECT, "pcb", "u-1", "root", "u1", "A")
        comments.create(PROJECT, "pcb", "u-1", "reply", "u2", "B",
                        parent_id=root["comment_id"])
        as_replier = comments.threads(PROJECT, viewer_id="u2")
        assert as_replier[0]["is_mine"] is True
        assert comments.threads(PROJECT, viewer_id="u9")[0]["is_mine"] is False

    def test_comment_emits_activity(self, comments, events):
        comments.create(PROJECT, "pcb", "u-1", "check this", "u1", "Aditya",
                        object_ref="R1")
        entry = events.recent(PROJECT)[0]
        assert entry["action"] == "comment_created"
        assert "R1" in entry["description"]

    def test_annotated_objects_badge_counts(self, comments):
        comments.create(PROJECT, "pcb", "u-1", "a", "u1", "A", object_ref="R1")
        comments.create(PROJECT, "pcb", "u-1", "b", "u1", "A", object_ref="R1")
        summary = comments.annotated_objects(PROJECT)
        assert summary["u-1"]["open"] == 2
