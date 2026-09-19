"""Integration tests: real WebSocket clients against a real server process.

These cover the behaviour the five-computer test exercises by hand, so a
regression is caught without setting up five machines.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import pytest
import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UID_R1 = "11111111-1111-4111-8111-111111111111"
UID_C1 = "22222222-2222-4222-8222-222222222222"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Start a real server subprocess on a free port."""
    port = free_port()
    data_dir = tmp_path_factory.mktemp("kicadlive_data")
    env = dict(os.environ, KICADLIVE_DATA_DIR=str(data_dir), PYTHONPATH=ROOT,
               PYTHONUNBUFFERED="1")

    # The server logs every lock, broadcast and connection. Piping that into an
    # undrained subprocess.PIPE fills the OS pipe buffer and blocks the server
    # mid-write, which looks exactly like a hung server. Log to a file instead.
    log_path = data_dir / "server.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True,
    )

    def server_log() -> str:
        log_file.flush()
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return "(log unavailable)"

    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server died on startup:\n{server_log()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(f"server did not start within 30s:\n{server_log()}")

    yield f"127.0.0.1:{port}"

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_file.close()


class Client:
    """Minimal protocol client used only by these tests."""

    def __init__(self, address: str, name: str, project: str, client_id: str):
        self.url = f"ws://{address}/ws"
        self.name = name
        self.project = project
        self.client_id = client_id
        self.ws = None
        self.versions: dict[str, int] = {}
        self.counter = 0

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self):
        self.ws = await websockets.connect(self.url)
        await self.send({"type": "hello", "client_id": self.client_id,
                         "user_name": self.name, "project_id": self.project})
        return await self.wait_for("project_state")

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def send(self, payload: dict):
        await self.ws.send(json.dumps(payload))

    async def wait_for(self, mtype: str, timeout: float = 6.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"{self.name}: never received {mtype!r}")
            raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            message = json.loads(raw)
            self._track(message)
            if message.get("type") == mtype:
                return message

    def _track(self, message: dict):
        if message.get("type") == "project_state":
            for uid, obj in (message.get("objects") or {}).items():
                self.versions[uid] = int(obj.get("version", 0))
        elif message.get("type") == "remote_change":
            for change in message.get("changes", []):
                self.versions[change["uuid"]] = int(change.get("version", 0))
        elif message.get("type") == "change_ack":
            for entry in message.get("accepted", []):
                self.versions[entry["uuid"]] = int(entry.get("version", 0))

    async def seed(self, objects):
        changes = [{
            "operation": "add", "object_type": "footprint", "uuid": uid, "reference": ref,
            "base_version": 0,
            "state": {"uuid": uid, "reference": ref, "object_type": "footprint",
                      "position": {"x": x, "y": y}, "rotation": 0.0,
                      "layer": "F.Cu", "value": ref},
        } for uid, ref, x, y in objects]
        self.counter += 1
        await self.send({"type": "change", "client_id": self.client_id,
                         "change_id": f"{self.client_id}-{self.counter}", "changes": changes})
        return await self.wait_for("change_ack")

    async def move(self, uid, ref, x, y, base_version=None, change_id=None):
        self.counter += 1
        cid = change_id or f"{self.client_id}-{self.counter}"
        await self.send({"type": "change", "client_id": self.client_id, "change_id": cid,
                         "changes": [{"operation": "modify", "object_type": "footprint",
                                      "uuid": uid, "reference": ref, "field": "position",
                                      "base_version": (self.versions.get(uid, 0)
                                                       if base_version is None else base_version),
                                      "old": None, "new": {"x": x, "y": y}}]})
        return cid


async def fresh(server, project: str, *names):
    """Connect N clients to a fresh project and seed it from the first."""
    clients = []
    for i, name in enumerate(names):
        client = Client(server, name, project, f"c{i}-{project}"[:64])
        await client.connect()
        clients.append(client)
    await clients[0].seed([(UID_R1, "R1", 60.0, 50.0), (UID_C1, "C1", 80.0, 50.0)])
    # Let every other client absorb the seed broadcast.
    for other in clients[1:]:
        await other.wait_for("remote_change")
    return clients


# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_endpoint(server):
    import httpx
    async with httpx.AsyncClient() as http:
        response = await http.get(f"http://{server}/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_two_clients_connect_and_see_each_other(server):
    a, b = await fresh(server, "p_connect", "Aditya", "Rahul")
    try:
        await a.send({"type": "presence", "client_id": a.client_id,
                      "activity": "editing", "selection": ["R1"], "kicad_connected": True})
        update = await b.wait_for("presence_update")
        names = {c["user_name"] for c in update["clients"] if c["online"]}
        assert names == {"Aditya", "Rahul"}
        aditya = next(c for c in update["clients"] if c["user_name"] == "Aditya")
        assert aditya["status_text"] == "Editing R1"
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_change_propagates_a_to_b(server):
    """Milestone 3: the core sync guarantee."""
    a, b = await fresh(server, "p_sync", "Aditya", "Rahul")
    try:
        await a.move(UID_R1, "R1", 65.0, 50.0)
        await a.wait_for("change_ack")
        remote = await b.wait_for("remote_change")
        change = remote["changes"][0]
        assert change["reference"] == "R1"
        assert change["new"] == {"x": 65.0, "y": 50.0}
        assert remote["origin_user_name"] == "Aditya"
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_originator_does_not_receive_its_own_change(server):
    """The loop breaker: a change must never come back to its sender."""
    a, b = await fresh(server, "p_noecho", "Aditya", "Rahul")
    try:
        await a.move(UID_R1, "R1", 70.0, 50.0)
        await a.wait_for("change_ack")
        await b.wait_for("remote_change")
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await a.wait_for("remote_change", timeout=1.5)
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_lock_granted_then_denied(server):
    a, b = await fresh(server, "p_lock", "Aditya", "Rahul")
    try:
        await a.send({"type": "lock_request", "client_id": a.client_id,
                      "uuid": UID_R1, "reference": "R1"})
        assert (await a.wait_for("lock_granted"))["uuid"] == UID_R1

        await b.send({"type": "lock_request", "client_id": b.client_id,
                      "uuid": UID_R1, "reference": "R1"})
        denied = await b.wait_for("lock_denied")
        assert denied["owner_name"] == "Aditya"

        await a.send({"type": "lock_release", "client_id": a.client_id, "uuid": UID_R1})
        await b.wait_for("lock_update")
        await b.send({"type": "lock_request", "client_id": b.client_id,
                      "uuid": UID_R1, "reference": "R1"})
        assert (await b.wait_for("lock_granted"))["owner"] == b.client_id
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_locked_object_rejects_other_clients_change(server):
    a, b = await fresh(server, "p_lockedit", "Aditya", "Rahul")
    try:
        await a.send({"type": "lock_request", "client_id": a.client_id,
                      "uuid": UID_R1, "reference": "R1"})
        await a.wait_for("lock_granted")

        await b.move(UID_R1, "R1", 99.0, 50.0)
        ack = await b.wait_for("change_ack")
        assert ack["accepted"] == []
        assert ack["rejected"][0]["reason"] == "locked"
        assert ack["rejected"][0]["owner_name"] == "Aditya"
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_concurrent_edit_produces_conflict(server):
    """Both clients edit R1 from the same base version; the loser is told."""
    a, b = await fresh(server, "p_conflict", "Aditya", "Rahul")
    try:
        base = a.versions.get(UID_R1, 0)
        await a.move(UID_R1, "R1", 45.0, 30.0, base_version=base)
        await a.wait_for("change_ack")
        await b.move(UID_R1, "R1", 40.0, 35.0, base_version=base)
        conflict = await b.wait_for("conflict")
        entry = conflict["conflicts"][0]
        assert entry["reference"] == "R1"
        assert entry["field"] == "position"
        assert entry["conflicting_user"] == "Aditya"
        assert entry["server_value"] == {"x": 45.0, "y": 30.0}
        assert entry["your_value"] == {"x": 40.0, "y": 35.0}
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_conflict_resolve_keep_mine_wins(server):
    a, b = await fresh(server, "p_resolve", "Aditya", "Rahul")
    try:
        base = a.versions.get(UID_R1, 0)
        await a.move(UID_R1, "R1", 45.0, 30.0, base_version=base)
        await a.wait_for("change_ack")
        await b.move(UID_R1, "R1", 40.0, 35.0, base_version=base)
        await b.wait_for("conflict")

        await b.send({"type": "conflict_resolve", "client_id": b.client_id,
                      "change_id": "resolve-1", "resolution": "keep_mine",
                      "uuid": UID_R1, "reference": "R1", "field": "position",
                      "value": {"x": 40.0, "y": 35.0}})
        await b.wait_for("change_ack")
        remote = await a.wait_for("remote_change")
        assert remote["changes"][0]["new"] == {"x": 40.0, "y": 35.0}
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_duplicate_change_id_is_applied_once(server):
    a, b = await fresh(server, "p_dupe", "Aditya", "Rahul")
    try:
        await a.move(UID_R1, "R1", 66.0, 50.0, change_id="fixed-id")
        await a.wait_for("change_ack")
        await b.wait_for("remote_change")

        await a.move(UID_R1, "R1", 66.0, 50.0, change_id="fixed-id")
        ack = await a.wait_for("change_ack")
        assert ack["duplicate"] is True
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await b.wait_for("remote_change", timeout=1.5)
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_disconnect_releases_locks_and_updates_presence(server):
    a, b = await fresh(server, "p_disc", "Aditya", "Rahul")
    try:
        await a.send({"type": "lock_request", "client_id": a.client_id,
                      "uuid": UID_R1, "reference": "R1"})
        await a.wait_for("lock_granted")
        await b.wait_for("lock_update")

        await a.close()

        # B must see the lock table empty and Aditya offline.
        deadline = time.time() + 6
        saw_empty_locks = saw_offline = False
        while time.time() < deadline and not (saw_empty_locks and saw_offline):
            message = await b.wait_for_any(timeout=2.0) if hasattr(b, "wait_for_any") else json.loads(
                await asyncio.wait_for(b.ws.recv(), timeout=2.0))
            if message.get("type") == "lock_update" and message["locks"] == []:
                saw_empty_locks = True
            if message.get("type") == "presence_update":
                aditya = next((c for c in message["clients"] if c["user_name"] == "Aditya"), None)
                if aditya and not aditya["online"]:
                    saw_offline = True
        assert saw_empty_locks, "locks were not released on disconnect"
        assert saw_offline, "presence did not mark the client offline"
    finally:
        await b.close()


@pytest.mark.asyncio
async def test_reconnect_restores_state(server):
    a, b = await fresh(server, "p_recon", "Aditya", "Rahul")
    try:
        await a.move(UID_R1, "R1", 77.0, 50.0)
        await a.wait_for("change_ack")
        await b.wait_for("remote_change")
        await b.close()

        # Work continues while B is away.
        await a.move(UID_C1, "C1", 88.0, 50.0)
        await a.wait_for("change_ack")

        b2 = Client(server, "Rahul", "p_recon", b.client_id)
        state = await b2.connect()
        try:
            assert state["objects"][UID_R1]["position"] == {"x": 77.0, "y": 50.0}
            assert state["objects"][UID_C1]["position"] == {"x": 88.0, "y": 50.0}
        finally:
            await b2.close()
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_five_clients_all_receive_every_change(server):
    """Test C of the five-computer procedure, run in one process."""
    names = ["Server-Op", "Designer A", "Designer B", "Designer C", "Designer D"]
    clients = await fresh(server, "p_five", *names)
    try:
        assert len(clients) == 5
        mover = clients[1]
        await mover.move(UID_R1, "R1", 55.5, 50.0)
        await mover.wait_for("change_ack")
        for other in clients:
            if other is mover:
                continue
            remote = await other.wait_for("remote_change")
            assert remote["changes"][0]["new"] == {"x": 55.5, "y": 50.0}
    finally:
        for client in clients:
            await client.close()


@pytest.mark.asyncio
async def test_invalid_messages_do_not_kill_the_connection(server):
    a, = await fresh(server, "p_bad", "Aditya")
    try:
        await a.send({"type": "not_a_real_type"})
        assert (await a.wait_for("error"))["code"] == "unknown_type"

        await a.send({"type": "lock_request", "client_id": a.client_id, "uuid": "../../etc/passwd"})
        assert (await a.wait_for("error"))["code"] == "invalid_id"

        await a.send({"type": "change", "client_id": a.client_id,
                      "change_id": "x", "changes": [
                          {"operation": "modify", "uuid": UID_R1, "field": "evil"}]})
        assert (await a.wait_for("error"))["code"] == "invalid_message"

        # The connection must still work afterwards.
        await a.send({"type": "heartbeat", "client_id": a.client_id})
        await a.wait_for("heartbeat")
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_message_before_hello_is_refused(server):
    ws = await websockets.connect(f"ws://{server}/ws")
    try:
        await ws.send(json.dumps({"type": "heartbeat", "client_id": "nope"}))
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert message["code"] == "not_registered"
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_history_records_accepted_changes(server):
    a, = await fresh(server, "p_hist", "Aditya")
    try:
        await a.move(UID_R1, "R1", 61.0, 50.0)
        await a.wait_for("change_ack")
        await a.send({"type": "request_history", "client_id": a.client_id, "limit": 10})
        history = await a.wait_for("history")
        entries = [e for e in history["entries"] if e.get("reference") == "R1"]
        assert entries, "no history entry for R1"
        assert entries[0]["user_name"] == "Aditya"
        assert "R1" in entries[0]["summary"]
    finally:
        await a.close()
