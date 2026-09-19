"""A KiCad-free WebSocket test client.

Lets you exercise and demonstrate the whole server — presence, locks, changes,
conflicts, history — on a machine with no KiCad installed. Also used as the
backup demo path if live KiCad sync misbehaves.

Examples:
    python tools/test_client.py --server 192.168.1.50 --name "Designer A"
    python tools/test_client.py --server 192.168.1.50 --name B --script move
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid as uuidlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from common.protocol import AGENT_VERSION, HEARTBEAT_INTERVAL

FAKE_OBJECTS = [
    ("11111111-1111-4111-8111-111111111111", "R1", 60.0, 50.0),
    ("22222222-2222-4222-8222-222222222222", "C1", 80.0, 50.0),
    ("33333333-3333-4333-8333-333333333333", "U1", 100.0, 55.0),
    ("44444444-4444-4444-8444-444444444444", "J1", 125.0, 55.0),
]


class TestClient:
    def __init__(self, url: str, name: str, project: str, client_id: str | None = None):
        self.url = url
        self.name = name
        self.project = project
        self.client_id = client_id or uuidlib.uuid4().hex[:8]
        self.ws = None
        self.versions: dict[str, int] = {}
        self.counter = 0
        # Unique per run: the server treats a repeated change_id as a replay.
        self.run_token = uuidlib.uuid4().hex[:6]

    async def connect(self):
        self.ws = await websockets.connect(self.url, max_size=8 * 1024 * 1024)
        await self.send({
            "type": "hello", "client_id": self.client_id, "user_name": self.name,
            "project_id": self.project, "agent_version": AGENT_VERSION,
        })
        print(f"[{self.name}] connected as {self.client_id}")

    async def send(self, payload: dict):
        await self.ws.send(json.dumps(payload))

    async def recv(self, timeout: float = 5.0) -> dict:
        raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        return json.loads(raw)

    async def wait_for(self, mtype: str, timeout: float = 5.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            message = await self.recv(timeout=max(0.1, deadline - loop.time()))
            self._track(message)
            if message.get("type") == mtype:
                return message
        raise TimeoutError(f"{self.name}: never received {mtype!r}")

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

    async def seed(self):
        """Populate an empty project with the fake objects."""
        changes = [{
            "operation": "add", "object_type": "footprint", "uuid": uid, "reference": ref,
            "base_version": 0,
            "state": {"uuid": uid, "reference": ref, "object_type": "footprint",
                      "position": {"x": x, "y": y}, "rotation": 0.0,
                      "layer": "F.Cu", "value": ref},
        } for uid, ref, x, y in FAKE_OBJECTS]
        self.counter += 1
        await self.send({"type": "change", "client_id": self.client_id,
                         "change_id": f"{self.client_id}-{self.run_token}-{self.counter}", "changes": changes})
        return await self.wait_for("change_ack")

    async def move(self, uid: str, ref: str, x: float, y: float, base_version: int | None = None):
        self.counter += 1
        change_id = f"{self.client_id}-{self.run_token}-{self.counter}"
        await self.send({
            "type": "change", "client_id": self.client_id, "change_id": change_id,
            "changes": [{
                "operation": "modify", "object_type": "footprint", "uuid": uid,
                "reference": ref, "field": "position",
                "base_version": self.versions.get(uid, 0) if base_version is None else base_version,
                "old": None, "new": {"x": x, "y": y},
            }],
        })
        return change_id

    async def lock(self, uid: str, ref: str):
        await self.send({"type": "lock_request", "client_id": self.client_id,
                         "uuid": uid, "reference": ref})

    async def unlock(self, uid: str | None = None):
        payload = {"type": "lock_release", "client_id": self.client_id}
        if uid:
            payload["uuid"] = uid
        await self.send(payload)

    async def presence(self, activity: str, selection: list[str]):
        await self.send({"type": "presence", "client_id": self.client_id,
                         "activity": activity, "selection": selection,
                         "kicad_connected": True})

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self.send({"type": "heartbeat", "client_id": self.client_id})
            except Exception:
                return

    async def close(self):
        if self.ws:
            await self.ws.close()


async def interactive(client: TestClient):
    await client.connect()
    asyncio.create_task(client.heartbeat_loop())
    print("Listening. Ctrl+C to stop.\n")
    while True:
        message = await client.recv(timeout=3600)
        client._track(message)
        mtype = message.get("type")
        if mtype == "presence_update":
            names = [f"{c['user_name']} ({c['status_text']})"
                     for c in message["clients"] if c["online"]]
            print(f"[{client.name}] presence: {', '.join(names) or 'nobody'}")
        elif mtype == "remote_change":
            for change in message.get("changes", []):
                print(f"[{client.name}] remote: {message.get('origin_user_name')} "
                      f"changed {change.get('reference')} {change.get('field')} "
                      f"-> {change.get('new')}")
        elif mtype == "lock_update":
            shown = [f"{lk['reference']}->{lk['owner_name']}" for lk in message["locks"]]
            print(f"[{client.name}] locks: {', '.join(shown) or 'none'}")
        elif mtype in ("lock_granted", "lock_denied", "conflict", "error"):
            print(f"[{client.name}] {mtype}: {json.dumps(message, indent=2)}")


async def script_move(client: TestClient):
    """Connect, seed if needed, then nudge R1 a few times."""
    await client.connect()
    welcome = await client.wait_for("welcome")
    await client.wait_for("project_state")
    if welcome.get("needs_seed"):
        print(f"[{client.name}] seeding project")
        await client.seed()
    uid, ref = FAKE_OBJECTS[0][0], FAKE_OBJECTS[0][1]
    for i in range(3):
        x = 60.0 + (i + 1) * 2.5
        await client.move(uid, ref, x, 50.0)
        ack = await client.wait_for("change_ack")
        print(f"[{client.name}] moved {ref} to x={x} -> accepted={len(ack['accepted'])}")
        await asyncio.sleep(1.0)
    await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="KiCad Live test client (no KiCad needed)")
    parser.add_argument("--server", default="127.0.0.1", help="server IP or host")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--name", default="Test Client")
    parser.add_argument("--project", default="demo_board")
    parser.add_argument("--script", choices=["move"], help="run a scripted scenario instead of listening")
    args = parser.parse_args()

    url = f"ws://{args.server}:{args.port}/ws"
    client = TestClient(url, args.name, args.project)
    runner = script_move if args.script == "move" else interactive
    try:
        asyncio.run(runner(client))
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
