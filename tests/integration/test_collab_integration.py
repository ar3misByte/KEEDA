"""Integration tests for schematic reporting, comments and session summaries.

Real server process, real WebSockets, real SQLite - no KiCad needed.
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
SAMPLE_DIR = os.path.join(ROOT, "sample_project")

UID_R1 = "11111111-1111-4111-8111-111111111111"
SCH_R1 = "sch-r1-uuid"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    data_dir = tmp_path_factory.mktemp("collab_data")
    env = dict(os.environ, KICADLIVE_DATA_DIR=str(data_dir),
               KICADLIVE_PROJECT_DIR=SAMPLE_DIR, PYTHONPATH=ROOT,
               PYTHONUNBUFFERED="1")
    log_path = data_dir / "server.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True)

    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            log_file.flush()
            pytest.fail("server died:\n" + log_path.read_text(errors="replace")[-3000:])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("server did not start")

    yield f"127.0.0.1:{port}"

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_file.close()


class Client:
    def __init__(self, address, name, project, client_id, role="designer"):
        self.url = f"ws://{address}/ws"
        self.name = name
        self.project = project
        self.client_id = client_id
        self.role = role
        self.ws = None
        self.counter = 0
        self.run_token = os.urandom(3).hex()

    async def connect(self):
        self.ws = await websockets.connect(self.url, open_timeout=10)
        await self.send({"type": "hello", "client_id": self.client_id,
                         "user_name": self.name, "project_id": self.project,
                         "role": self.role})
        return await self.wait_for("project_state")

    async def send(self, payload):
        await self.ws.send(json.dumps(payload))

    async def wait_for(self, mtype, timeout=8.0, predicate=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"{self.name}: never received {mtype!r}")
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            if msg.get("type") == mtype and (predicate is None or predicate(msg)):
                return msg

    async def schematic_change(self, uuid, reference, field, new, operation="modify"):
        self.counter += 1
        await self.send({
            "type": "change", "client_id": self.client_id,
            "change_id": f"{self.client_id}-{self.run_token}-{self.counter}",
            "changes": [{"operation": operation, "domain": "schematic",
                         "object_type": "symbol", "uuid": uuid, "reference": reference,
                         "sheet": "/", "field": field, "old": None, "new": new,
                         "base_version": 0}]})

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None


async def pair(server, project, *names):
    clients = []
    for i, name in enumerate(names):
        c = Client(server, name, project, f"c{i}-{project}"[:64])
        await c.connect()
        clients.append(c)
    return clients


# --------------------------------------------------------------- schematic

@pytest.mark.asyncio
async def test_schematic_change_is_broadcast_to_others(server):
    a, b = await pair(server, "sch_bcast", "Aditya", "Rahul")
    try:
        await a.schematic_change(SCH_R1, "R1", "value", "4k7")
        remote = await b.wait_for("remote_change")
        change = remote["changes"][0]
        assert change["domain"] == "schematic"
        assert change["reference"] == "R1"
        assert change["new"] == "4k7"
        assert remote["origin_user_name"] == "Aditya"
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_schematic_change_becomes_activity(server):
    a, = await pair(server, "sch_activity", "Aditya")
    try:
        await a.schematic_change(SCH_R1, "R1", "value", "10k")
        await a.send({"type": "request_activity", "client_id": a.client_id, "limit": 20})
        feed = await a.wait_for("activity", predicate=lambda m: any(
            e.get("domain") == "schematic" for e in m.get("events", [])))
        entry = next(e for e in feed["events"] if e["domain"] == "schematic")
        assert "R1" in entry["description"]
        assert entry["username"] == "Aditya"
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_schematic_lock_blocks_another_users_edit(server):
    a, b = await pair(server, "sch_lock", "Aditya", "Rahul")
    try:
        await a.send({"type": "lock_request", "client_id": a.client_id,
                      "uuid": SCH_R1, "reference": "R1", "domain": "schematic",
                      "object_type": "symbol"})
        assert (await a.wait_for("lock_granted"))["domain"] == "schematic"

        await b.schematic_change(SCH_R1, "R1", "value", "1k")
        denied = await b.wait_for("lock_denied")
        assert denied["owner_name"] == "Aditya"
        assert denied["domain"] == "schematic"
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_schematic_and_pcb_locks_are_independent(server):
    """Same reference, different domains - two people may legitimately work."""
    a, b = await pair(server, "sch_pcb_lock", "Aditya", "Rahul")
    try:
        await a.send({"type": "lock_request", "client_id": a.client_id,
                      "uuid": UID_R1, "reference": "R1", "domain": "schematic"})
        await a.wait_for("lock_granted")
        await b.send({"type": "lock_request", "client_id": b.client_id,
                      "uuid": UID_R1, "reference": "R1", "domain": "pcb"})
        granted = await b.wait_for("lock_granted")
        assert granted["owner"] == b.client_id
    finally:
        await a.close(); await b.close()


# ---------------------------------------------------------------- comments

@pytest.mark.asyncio
async def test_comment_is_created_and_broadcast(server):
    a, b = await pair(server, "cmt_create", "Aditya", "Rahul")
    try:
        await a.send({"type": "comment_create", "domain": "schematic",
                      "object_id": SCH_R1, "object_type": "symbol", "object_ref": "R1",
                      "text": "Should this be 4.7k instead of 10k?"})
        created = await b.wait_for("comment_created")
        assert created["comment"]["object_ref"] == "R1"
        assert created["comment"]["author_name"] == "Aditya"
        assert created["counts"]["open"] == 1
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_reply_and_resolve(server):
    a, b = await pair(server, "cmt_thread", "Aditya", "Rahul")
    try:
        await a.send({"type": "comment_create", "domain": "schematic",
                      "object_id": SCH_R1, "object_ref": "R1", "object_type": "symbol",
                      "text": "Check this resistor value."})
        created = await a.wait_for("comment_created")
        root_id = created["comment"]["comment_id"]

        await b.send({"type": "comment_create", "domain": "schematic",
                      "object_id": SCH_R1, "parent_id": root_id,
                      "text": "Yes - it is the I2C pull-up."})
        await a.wait_for("comment_created")

        await a.send({"type": "request_comments", "client_id": a.client_id, "status": "all"})
        listing = await a.wait_for("comments")
        thread = next(t for t in listing["threads"] if t["comment_id"] == root_id)
        assert thread["reply_count"] == 1
        assert thread["replies"][0]["author_name"] == "Rahul"

        await b.send({"type": "comment_status", "comment_id": root_id, "status": "resolved"})
        updated = await a.wait_for("comment_updated")
        assert updated["comment"]["status"] == "resolved"
        assert updated["counts"]["open"] == 0
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_empty_comment_is_refused(server):
    a, = await pair(server, "cmt_bad", "Aditya")
    try:
        await a.send({"type": "comment_create", "domain": "schematic",
                      "object_id": SCH_R1, "text": "   "})
        assert (await a.wait_for("error"))["code"] == "invalid_message"
        # The connection must survive a rejected comment.
        await a.send({"type": "heartbeat", "client_id": a.client_id})
        await a.wait_for("heartbeat")
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_comment_survives_server_view_reload(server):
    """Comments are persisted, so a fresh client sees them on connect."""
    a, = await pair(server, "cmt_persist", "Aditya")
    try:
        await a.send({"type": "comment_create", "domain": "pcb", "object_id": UID_R1,
                      "object_ref": "R1", "text": "Keep this away from the edge."})
        await a.wait_for("comment_created")
    finally:
        await a.close()

    later = Client(server, "Rahul", "cmt_persist", "late-joiner")
    await later.connect()
    try:
        listing = await later.wait_for("comments")
        assert any(t["object_ref"] == "R1" for t in listing["threads"])
    finally:
        await later.close()


# ------------------------------------------------- what changed since I left

@pytest.mark.asyncio
async def test_first_visit_is_marked(server):
    a, = await pair(server, "new_first", "Aditya")
    try:
        summary = await a.wait_for("whats_new")
        assert summary["summary"]["is_first_visit"] is True
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_whats_new_reports_other_peoples_changes(server):
    a, b = await pair(server, "new_delta", "Aditya", "Rahul")
    try:
        await a.send({"type": "mark_read", "client_id": a.client_id})
        await a.wait_for("marked_read")

        await b.schematic_change(SCH_R1, "R1", "value", "220R")
        await b.send({"type": "comment_create", "domain": "schematic",
                      "object_id": SCH_R1, "object_ref": "R1", "text": "changed it"})
        await b.wait_for("comment_created")
        await asyncio.sleep(0.4)

        await a.send({"type": "request_whats_new", "client_id": a.client_id})
        payload = (await a.wait_for("whats_new"))["summary"]
        assert payload["counts"]["schematic"] >= 1
        assert payload["counts"]["comments"] >= 1
        assert any(c["username"] == "Rahul" for c in payload["contributors"])
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_own_changes_are_not_in_whats_new(server):
    a, = await pair(server, "new_self", "Aditya")
    try:
        await a.send({"type": "mark_read", "client_id": a.client_id})
        await a.wait_for("marked_read")
        await a.schematic_change(SCH_R1, "R1", "value", "47R")
        await asyncio.sleep(0.4)
        await a.send({"type": "request_whats_new", "client_id": a.client_id})
        payload = (await a.wait_for("whats_new"))["summary"]
        assert payload["total"] == 0, "a user's own edits are not 'what changed'"
    finally:
        await a.close()


# ------------------------------------------------------------- REST surface

@pytest.mark.asyncio
async def test_rest_endpoints_respond(server):
    import httpx
    async with httpx.AsyncClient(base_url=f"http://{server}") as http:
        for path in ["/api/overview", "/api/activity", "/api/comments",
                     "/api/users", "/api/versions", "/api/schematic", "/api/pcb"]:
            response = await http.get(path, params={"project": "rest_probe"})
            assert response.status_code == 200, f"{path} -> {response.status_code}"


@pytest.mark.asyncio
async def test_summary_endpoint_is_offline_and_honest(server):
    """The hardware summary must work with no network and must not invent USB."""
    import httpx
    async with httpx.AsyncClient(base_url=f"http://{server}", timeout=60) as http:
        response = await http.get("/api/summary", params={"project": "rest_probe"})
    payload = response.json()
    assert payload["available"] is True
    assert payload["offline"] is True
    interfaces = [i["name"] for i in payload["schematic"]["interfaces"]]
    assert "I2C" in interfaces
    assert "USB" not in interfaces, "the sample board has no USB; it must not be claimed"


@pytest.mark.asyncio
async def test_bad_project_id_is_rejected(server):
    import httpx
    async with httpx.AsyncClient(base_url=f"http://{server}") as http:
        response = await http.get("/api/overview", params={"project": "../etc/passwd"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_dashboard_pages_serve(server):
    import httpx
    async with httpx.AsyncClient(base_url=f"http://{server}") as http:
        for page in ["/", "/index.html", "/activity.html", "/comments.html",
                     "/project.html", "/schematic.html", "/pcb.html",
                     "/static/app.js", "/static/style.css"]:
            response = await http.get(page)
            assert response.status_code == 200, f"{page} -> {response.status_code}"
