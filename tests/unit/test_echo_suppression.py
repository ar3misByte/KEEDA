"""Tests for the agent's loop-breaking and lock-enforcement logic.

These use a fake KiCad link and a fake socket, so they run anywhere - no KiCad
and no server required.
"""
import asyncio

import pytest

from agent.sync_agent import SyncAgent

UID = "11111111-1111-4111-8111-111111111111"


def fp(x=10.0, y=20.0, ref="R1", rot=0.0):
    return {"uuid": UID, "reference": ref, "object_type": "footprint",
            "position": {"x": x, "y": y}, "rotation": rot,
            "layer": "F.Cu", "value": "10k"}


class FakeLink:
    """Stands in for KiCad: a snapshot you can poke, and a record of writes."""

    def __init__(self, snapshot=None):
        self.snapshot = snapshot if snapshot is not None else {UID: fp()}
        self.applied = []
        self.selection = {}

    def read_snapshot(self):
        import copy
        return copy.deepcopy(self.snapshot)

    def apply_changes(self, changes, description=""):
        self.applied.append((description, changes))
        for change in changes:
            if change.get("operation") == "modify":
                self.snapshot[change["uuid"]][change["field"]] = change["new"]
        return len(changes)

    def selection_uuids(self):
        return dict(self.selection)


class FakeWS:
    def __init__(self, client_id="me"):
        self.client_id = client_id
        self.sent = []
        self.incoming = asyncio.Queue()
        self.connected = asyncio.Event()
        self.connected.set()

    async def send(self, payload):
        self.sent.append(payload)
        return True

    def changes_sent(self):
        return [c for m in self.sent if m.get("type") == "change" for c in m["changes"]]


def make_agent(link=None, **kwargs):
    link = link or FakeLink()
    ws = FakeWS()
    agent = SyncAgent(link, ws, "Tester", **kwargs)
    agent.baseline = link.read_snapshot()
    agent.state_ready.set()
    return agent, link, ws


class TestEchoSuppression:
    @pytest.mark.asyncio
    async def test_remote_change_is_not_sent_back(self):
        agent, link, ws = make_agent()
        await agent._on_remote_change({
            "origin_user_name": "Other",
            "changes": [{"operation": "modify", "object_type": "footprint", "uuid": UID,
                         "reference": "R1", "field": "position",
                         "new": {"x": 50.0, "y": 20.0}, "version": 3}]})
        assert link.applied, "the remote change should have been applied to KiCad"

        await agent.poll_once()
        assert ws.changes_sent() == [], "the applied change must not be echoed back"

    @pytest.mark.asyncio
    async def test_echo_mark_expires_so_later_real_edits_still_send(self):
        """Regression: a stale echo mark used to swallow the next genuine edit."""
        agent, link, ws = make_agent()
        await agent._on_remote_change({
            "origin_user_name": "Other",
            "changes": [{"operation": "modify", "object_type": "footprint", "uuid": UID,
                         "reference": "R1", "field": "position",
                         "new": {"x": 50.0, "y": 20.0}, "version": 3}]})

        # Two quiet polls: nothing to report, and the mark must age out.
        await agent.poll_once()
        await agent.poll_once()
        assert ws.changes_sent() == []
        assert UID not in agent.echo_marks

        # Now the user really moves it.
        link.snapshot[UID]["position"] = {"x": 77.0, "y": 20.0}
        await agent.poll_once()

        sent = ws.changes_sent()
        assert len(sent) == 1, "the user's own edit must be shared"
        assert sent[0]["new"] == {"x": 77.0, "y": 20.0}

    @pytest.mark.asyncio
    async def test_local_edit_is_sent_with_base_version(self):
        agent, link, ws = make_agent()
        agent.field_versions[(UID, "position")] = 7
        link.snapshot[UID]["position"] = {"x": 33.0, "y": 20.0}
        await agent.poll_once()

        sent = ws.changes_sent()
        assert len(sent) == 1
        assert sent[0]["base_version"] == 7

    @pytest.mark.asyncio
    async def test_no_change_sends_nothing(self):
        agent, link, ws = make_agent()
        await agent.poll_once()
        await agent.poll_once()
        assert ws.sent == []


class TestLockEnforcement:
    @pytest.mark.asyncio
    async def test_edit_to_locked_object_is_reverted_and_not_sent(self):
        agent, link, ws = make_agent()
        agent.locks = {UID: {"uuid": UID, "reference": "R1",
                             "owner": "someone-else", "owner_name": "Rahul"}}

        link.snapshot[UID]["position"] = {"x": 99.0, "y": 20.0}
        await agent.poll_once()

        assert ws.changes_sent() == [], "a locked component's edit must not be shared"
        assert link.snapshot[UID]["position"] == {"x": 10.0, "y": 20.0}, "it must be put back"
        assert agent.stats["blocked"] == 1

    @pytest.mark.asyncio
    async def test_revert_does_not_block_a_later_edit_once_unlocked(self):
        """The exact sequence the live lock test exercises."""
        agent, link, ws = make_agent()
        agent.locks = {UID: {"uuid": UID, "reference": "R1",
                             "owner": "someone-else", "owner_name": "Rahul"}}
        link.snapshot[UID]["position"] = {"x": 99.0, "y": 20.0}
        await agent.poll_once()
        assert ws.changes_sent() == []

        # Lock released, marks age out.
        agent.locks = {}
        await agent.poll_once()
        await agent.poll_once()

        link.snapshot[UID]["position"] = {"x": 44.0, "y": 20.0}
        await agent.poll_once()

        sent = ws.changes_sent()
        assert len(sent) == 1, "the edit must go through once the lock is gone"
        assert sent[0]["new"] == {"x": 44.0, "y": 20.0}

    @pytest.mark.asyncio
    async def test_own_lock_does_not_block_me(self):
        agent, link, ws = make_agent()
        agent.locks = {UID: {"uuid": UID, "reference": "R1",
                             "owner": "me", "owner_name": "Tester"}}
        link.snapshot[UID]["position"] = {"x": 55.0, "y": 20.0}
        await agent.poll_once()
        assert len(ws.changes_sent()) == 1


class TestConflictHandling:
    @pytest.mark.asyncio
    async def test_conflict_reverts_to_the_server_value(self):
        agent, link, ws = make_agent()
        await agent._on_conflict({
            "change_id": "x",
            "conflicts": [{"uuid": UID, "reference": "R1", "field": "position",
                           "your_base_version": 1, "current_version": 5,
                           "your_value": {"x": 99.0, "y": 20.0},
                           "server_value": {"x": 42.0, "y": 20.0},
                           "conflicting_user": "Rahul"}]})
        assert link.snapshot[UID]["position"] == {"x": 42.0, "y": 20.0}
        assert agent.stats["conflicts"] == 1
        assert agent.field_versions[(UID, "position")] == 5

    @pytest.mark.asyncio
    async def test_conflict_revert_is_not_rebroadcast(self):
        agent, link, ws = make_agent()
        await agent._on_conflict({
            "change_id": "x",
            "conflicts": [{"uuid": UID, "reference": "R1", "field": "position",
                           "your_base_version": 1, "current_version": 5,
                           "your_value": {"x": 99.0, "y": 20.0},
                           "server_value": {"x": 42.0, "y": 20.0},
                           "conflicting_user": "Rahul"}]})
        await agent.poll_once()
        assert ws.changes_sent() == []


class TestReadOnlyMode:
    @pytest.mark.asyncio
    async def test_read_only_agent_never_sends_changes(self):
        agent, link, ws = make_agent(read_only=True)
        link.snapshot[UID]["position"] = {"x": 12.0, "y": 20.0}
        await agent.poll_once()
        assert ws.changes_sent() == []

    @pytest.mark.asyncio
    async def test_read_only_agent_still_applies_remote_changes(self):
        agent, link, ws = make_agent(read_only=True)
        await agent._on_remote_change({
            "origin_user_name": "Other",
            "changes": [{"operation": "modify", "object_type": "footprint", "uuid": UID,
                         "reference": "R1", "field": "position",
                         "new": {"x": 60.0, "y": 20.0}, "version": 2}]})
        assert link.snapshot[UID]["position"] == {"x": 60.0, "y": 20.0}


class TestSelectionAndPresence:
    @pytest.mark.asyncio
    async def test_selecting_a_part_requests_a_lock_and_updates_presence(self):
        agent, link, ws = make_agent()
        link.selection = {UID: "R1"}
        await agent.poll_selection()

        types = [m["type"] for m in ws.sent]
        assert "lock_request" in types
        presence = next(m for m in ws.sent if m["type"] == "presence")
        assert presence["activity"] == "editing"
        assert presence["selection"] == ["R1"]

    @pytest.mark.asyncio
    async def test_deselecting_releases_the_lock(self):
        agent, link, ws = make_agent()
        link.selection = {UID: "R1"}
        await agent.poll_selection()
        agent.my_locks.add(UID)

        link.selection = {}
        ws.sent.clear()
        await agent.poll_selection()

        releases = [m for m in ws.sent if m["type"] == "lock_release"]
        assert releases and releases[0]["uuid"] == UID


class TestChangeIds:
    def test_change_ids_differ_across_agent_restarts(self):
        """Regression: a restarted agent reused ids, so the server's duplicate
        guard silently discarded its first changes as replays."""
        first, _, _ = make_agent()
        second, _, _ = make_agent()          # same client_id, new process
        assert first.ws.client_id == second.ws.client_id
        assert first._next_change_id() != second._next_change_id()

    def test_change_ids_are_unique_within_a_run(self):
        agent, _, _ = make_agent()
        ids = [agent._next_change_id() for _ in range(50)]
        assert len(set(ids)) == 50
