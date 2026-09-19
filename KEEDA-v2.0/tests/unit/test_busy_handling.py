"""Regression tests: a busy KiCad must never stall, corrupt or disconnect the agent.

KiCad answers "busy" while a dialog is open, while the user is mid-drag, or
while it is loading a board. Before these tests existed that caused:

  * the connect-time state sync to give up and never retry, so the machine
    never adopted existing work and never sent its own edits;
  * "busy" reported as a lost connection (presence flipped to offline);
  * an empty selection being read, releasing the user's locks;
  * a failed remote apply leaving a false baseline, which then sent the OLD
    value to the server as if the user had edited it.
"""
import asyncio

import pytest

from agent.kicad_link import KiCadBusy, KiCadLink, KiCadUnavailable, _is_busy, _translate
from agent.sync_agent import SyncAgent
from tests.unit.test_echo_suppression import UID, FakeLink, FakeWS, fp


class BusyLink(FakeLink):
    """A fake KiCad that can be flipped between busy and responsive."""

    def __init__(self, snapshot=None):
        super().__init__(snapshot)
        self.busy = False

    def _check(self):
        if self.busy:
            raise KiCadBusy("KiCad is busy and cannot respond to API requests now")

    def read_snapshot(self):
        self._check()
        return super().read_snapshot()

    def apply_changes(self, changes, description=""):
        self._check()
        return super().apply_changes(changes, description)

    def selection_uuids(self):
        self._check()
        return super().selection_uuids()


def make_agent():
    link = BusyLink()
    ws = FakeWS()
    agent = SyncAgent(link, ws, "Tester")
    agent.baseline = link.read_snapshot()
    agent.state_ready.set()
    return agent, link, ws


def server_state(x=50.0):
    return {"objects": {UID: {"uuid": UID, "reference": "R1", "position": {"x": x, "y": 20.0},
                              "rotation": 0.0, "layer": "F.Cu", "value": "10k",
                              "field_versions": {"position": 3}}},
            "locks": []}


class TestConnectTimeSync:
    @pytest.mark.asyncio
    async def test_busy_at_connect_stays_pending_instead_of_giving_up(self):
        agent, link, ws = make_agent()
        agent.state_ready.clear()
        agent.baseline = {}
        link.busy = True

        await agent._on_project_state(server_state())      # must not raise

        assert agent._pending_state is not None, "the state must be kept for retry"
        assert not agent.state_ready.is_set()

    @pytest.mark.asyncio
    async def test_pending_state_is_adopted_once_kicad_responds(self):
        agent, link, ws = make_agent()
        agent.state_ready.clear()
        agent.baseline = {}
        link.busy = True
        await agent._on_project_state(server_state(x=50.0))

        link.busy = False
        await agent._step()

        assert agent.state_ready.is_set()
        assert agent._pending_state is None
        assert link.snapshot[UID]["position"] == {"x": 50.0, "y": 20.0}, \
            "existing work from the server must reach this board"

    @pytest.mark.asyncio
    async def test_adopting_state_does_not_report_phantom_local_edits(self):
        """An empty baseline used to make every footprint look like a new local add."""
        agent, link, ws = make_agent()
        agent.state_ready.clear()
        agent.baseline = {}
        await agent._on_project_state(server_state(x=50.0))
        ws.sent.clear()

        await agent.poll_once()

        assert ws.changes_sent() == []

    @pytest.mark.asyncio
    async def test_poll_loop_retries_and_never_reports_offline(self):
        agent, link, ws = make_agent()
        agent.state_ready.clear()
        agent.baseline = {}
        agent.poll_interval = 0.01
        ws.connected.set()
        link.busy = True
        await agent._on_project_state(server_state())

        task = asyncio.create_task(agent.poll_loop())
        try:
            await asyncio.sleep(0.15)
            assert not agent.state_ready.is_set()
            assert not [m for m in ws.sent
                        if m.get("type") == "presence" and m.get("activity") == "offline"], \
                "a busy KiCad must not make this user look disconnected"

            link.busy = False
            for _ in range(200):
                if agent.state_ready.is_set():
                    break
                await asyncio.sleep(0.02)
            assert agent.state_ready.is_set(), "the loop must recover on its own"
            assert not task.done(), "the poll loop must survive"
        finally:
            task.cancel()


class TestApplyingRemoteChanges:
    remote = {"origin_user_name": "Other",
              "changes": [{"operation": "modify", "object_type": "footprint", "uuid": UID,
                           "reference": "R1", "field": "position",
                           "new": {"x": 50.0, "y": 20.0}, "version": 3}]}

    @pytest.mark.asyncio
    async def test_busy_apply_is_queued_and_baseline_stays_truthful(self):
        agent, link, ws = make_agent()
        link.busy = True
        await agent._on_remote_change(self.remote)

        assert len(agent._deferred) == 1
        assert agent.baseline[UID]["position"] == {"x": 10.0, "y": 20.0}, \
            "the baseline must still match what the board actually holds"

    @pytest.mark.asyncio
    async def test_queued_change_is_applied_and_does_not_bounce_back(self):
        agent, link, ws = make_agent()
        link.busy = True
        await agent._on_remote_change(self.remote)

        with pytest.raises(KiCadBusy):
            await agent._step()                            # still busy: stays queued
        assert len(agent._deferred) == 1

        link.busy = False
        await agent._step()

        assert link.snapshot[UID]["position"] == {"x": 50.0, "y": 20.0}
        assert agent._deferred == []
        await agent.poll_once()
        assert ws.changes_sent() == [], "the remote change must not be echoed to the server"

    @pytest.mark.asyncio
    async def test_a_failed_apply_never_sends_the_old_value_to_the_server(self):
        """The corruption bug: baseline claimed the new value, board still had the
        old one, and the next poll reported the old value as a local edit."""
        agent, link, ws = make_agent()
        link.busy = True
        await agent._on_remote_change(self.remote)
        link.busy = False

        for _ in range(5):                                # several polls, echo marks expire
            await agent.poll_once()
        assert ws.changes_sent() == []

    @pytest.mark.asyncio
    async def test_queued_changes_apply_in_order(self):
        agent, link, ws = make_agent()
        link.busy = True
        for x in (50.0, 60.0):
            change = dict(self.remote)
            change["changes"] = [dict(self.remote["changes"][0], new={"x": x, "y": 20.0})]
            await agent._on_remote_change(change)

        link.busy = False
        await agent._step()

        assert link.snapshot[UID]["position"] == {"x": 60.0, "y": 20.0}, \
            "a newer change must never be overwritten by an older queued one"

    @pytest.mark.asyncio
    async def test_reconnect_drops_stale_queued_work(self):
        agent, link, ws = make_agent()
        link.busy = True
        await agent._on_remote_change(self.remote)
        agent.on_reconnect()
        assert agent._deferred == [] and agent._pending_state is None


class TestSelectionAndLocks:
    @pytest.mark.asyncio
    async def test_busy_kicad_does_not_release_the_users_locks(self):
        agent, link, ws = make_agent()
        agent.selection = {UID: "R1"}
        agent.my_locks.add(UID)
        link.busy = True

        await agent.poll_selection()

        assert [m for m in ws.sent if m.get("type") == "lock_release"] == []
        assert agent.selection == {UID: "R1"}, "an unreadable selection is not an empty one"


class TestErrorClassification:
    def test_api_busy_status_is_busy(self):
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode
        exc = ApiError("KiCad returned error: kicad is busy and cannot respond to API "
                       "requests now", code=ApiStatusCode.AS_BUSY)
        assert _is_busy(exc)
        assert isinstance(_translate(exc, "x"), KiCadBusy)

    def test_timeouts_are_busy(self):
        assert _is_busy(ConnectionError("Error receiving reply from KiCad: Timed out"))

    def test_a_refused_connection_is_not_busy(self):
        exc = ConnectionError("Failed to send command to KiCad: Connection refused")
        assert not _is_busy(exc)
        translated = _translate(exc, "x")
        assert isinstance(translated, KiCadUnavailable) and not isinstance(translated, KiCadBusy)

    def test_link_raises_busy_from_the_board_and_the_selection(self):
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode

        class BusyBoard:
            def get_footprints(self):
                raise ApiError("kicad is busy", code=ApiStatusCode.AS_BUSY)

            def get_selection(self):
                raise ApiError("kicad is busy", code=ApiStatusCode.AS_BUSY)

        link = KiCadLink()
        link._board = BusyBoard()
        with pytest.raises(KiCadBusy):
            link.read_snapshot()
        with pytest.raises(KiCadBusy):
            link.selection_uuids()       # must raise, NOT return {}


class TestWhatsNewNotice:
    @pytest.mark.asyncio
    async def test_schematic_changes_are_listed_not_silently_missing(self, capsys):
        agent, link, ws = make_agent()
        await agent._on_whats_new({"summary": {
            "is_first_visit": False, "total": 2,
            "counts": {"schematic": 1, "pcb": 1, "comments": 0},
            "schematic": [{"username": "Rahul", "description": "changed R1 value from 4k7 to 10k"}]}})
        out = capsys.readouterr().out
        assert "R1 value" in out and "NOT applied" in out
        assert any(m.get("type") == "mark_read" for m in ws.sent)

    @pytest.mark.asyncio
    async def test_first_visit_stays_quiet(self, capsys):
        agent, link, ws = make_agent()
        await agent._on_whats_new({"summary": {"is_first_visit": True, "total": 5}})
        assert capsys.readouterr().out == "" and ws.sent == []


class TestUnnamedBoard:
    def test_board_without_a_name_is_not_ready_and_never_joins_default(self):
        """The reported bug: 'board ''' + project 'default', then endless busy."""
        import sys, types
        from unittest import mock

        class Board:
            name = ""

            def get_layer_name(self, i):
                return ""

        class FakeKiCad:
            def __init__(self, client_name=None):
                pass

            def get_board(self):
                return Board()

            def get_open_documents(self, t):
                return []

        with mock.patch("kipy.KiCad", FakeKiCad):
            with pytest.raises(KiCadBusy):
                KiCadLink().connect()
