"""Milestone 3 - end-to-end live sync against a REAL running KiCad.

This is the test that proves the whole chain works:

    KiCad  <->  agent  <->  server  <->  second client

It needs a human to set two things up first:

  1. KiCad open with sample_project/demo_board.kicad_pcb, API enabled.
  2. The sync server running:  python -m server.main

Then:

    python tests/manual/test_live_sync.py

It plays both roles itself: it starts a real agent process against your KiCad,
and connects a second, KiCad-free client to the server. A "local edit" is
simulated by moving a footprint through a separate IPC connection, which is
indistinguishable to the agent from you dragging it.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid as uuidlib
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import websockets

from agent.kicad_link import KiCadLink, KiCadUnavailable
from common.diff_engine import mm_to_nm

SERVER = os.environ.get("KICADLIVE_SERVER", "127.0.0.1")
PORT = int(os.environ.get("KICADLIVE_PORT", "8000"))
PROJECT = "demo_board"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(f"{'[PASS]' if ok else '[FAIL]'} {label}{(' - ' + detail) if detail else ''}", flush=True)
    return ok


class Peer:
    """The second designer, with no KiCad of their own."""

    def __init__(self, name="Designer B"):
        self.name = name
        self.client_id = "livetest-peer"
        # Unique per run, so the server never mistakes a new change for a replay.
        self.run_token = uuidlib.uuid4().hex[:6]
        self.ws = None
        self.versions: dict[str, int] = {}
        self.counter = 0

    async def connect(self):
        self.ws = await websockets.connect(f"ws://{SERVER}:{PORT}/ws", open_timeout=10)
        await self.ws.send(json.dumps({"type": "hello", "client_id": self.client_id,
                                       "user_name": self.name, "project_id": PROJECT}))
        return await self.wait_for("project_state", timeout=10)

    async def wait_for(self, mtype, timeout=10.0, predicate=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"never received {mtype}")
            raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            message = json.loads(raw)
            self._track(message)
            if message.get("type") == mtype and (predicate is None or predicate(message)):
                return message

    def _track(self, message):
        if message.get("type") == "project_state":
            for uid, obj in (message.get("objects") or {}).items():
                for field, version in (obj.get("field_versions") or {}).items():
                    self.versions[f"{uid}:{field}"] = int(version)
        elif message.get("type") == "remote_change":
            for change in message.get("changes", []):
                if change.get("field"):
                    self.versions[f"{change['uuid']}:{change['field']}"] = int(change.get("version", 0))
        elif message.get("type") == "change_ack":
            for entry in message.get("accepted", []):
                if entry.get("field"):
                    self.versions[f"{entry['uuid']}:{entry['field']}"] = int(entry.get("version", 0))

    async def move(self, uuid, reference, x, y):
        self.counter += 1
        await self.ws.send(json.dumps({
            "type": "change", "client_id": self.client_id,
            "change_id": f"{self.client_id}-{self.run_token}-{self.counter}",
            "changes": [{"operation": "modify", "object_type": "footprint", "uuid": uuid,
                         "reference": reference, "field": "position",
                         "base_version": self.versions.get(f"{uuid}:position", 0),
                         "old": None, "new": {"x": x, "y": y}}]}))

    async def close(self):
        if self.ws:
            await self.ws.close()


async def wait_until(predicate, timeout=15.0, interval=0.2):
    """Poll a synchronous predicate until it is true or time runs out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def main() -> int:
    print("=" * 62)
    print(" KiCad Live - live end-to-end sync test")
    print("=" * 62)

    # --- preconditions ----------------------------------------------------
    probe = KiCadLink(client_name_prefix="kl-livetest")
    try:
        board_name = probe.connect()
    except KiCadUnavailable as exc:
        check(False, "KiCad reachable", str(exc))
        print("\nOpen KiCad with sample_project/demo_board.kicad_pcb and enable the API.")
        return 1
    check(True, "KiCad reachable", board_name)

    snapshot = probe.read_snapshot()
    by_reference = {state["reference"]: state for state in snapshot.values()}
    if not check("R1" in by_reference and "C1" in by_reference,
                 "Demo board has R1 and C1", ", ".join(sorted(by_reference))):
        return 1
    r1_uuid = by_reference["R1"]["uuid"]
    c1_uuid = by_reference["C1"]["uuid"]

    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{SERVER}:{PORT}/health", timeout=5) as response:
            check(json.loads(response.read())["status"] == "ok", "Sync server reachable",
                  f"http://{SERVER}:{PORT}")
    except Exception as exc:
        check(False, "Sync server reachable", str(exc))
        print("\nStart it with:  python -m server.main")
        return 1

    # --- start a real agent against the running KiCad ----------------------
    log_path = os.path.join(ROOT, "data", "livetest-agent.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    agent_log = open(log_path, "w", encoding="utf-8")
    agent = subprocess.Popen(
        [sys.executable, "-m", "agent.main", "--server", SERVER, "--port", str(PORT),
         "--name", "Designer A", "--project", PROJECT, "--client-id", "livetest-agent"],
        cwd=ROOT, stdout=agent_log, stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONPATH=ROOT), text=True)

    peer = Peer()
    try:
        await asyncio.sleep(6)          # let the agent connect and seed
        if agent.poll() is not None:
            agent_log.flush()
            check(False, "Agent process stayed up",
                  open(log_path, encoding="utf-8", errors="replace").read()[-1500:])
            return 1
        check(True, "Agent process stayed up")

        state = await peer.connect()
        check(state["objects"], "Peer received project state",
              f"{len(state['objects'])} objects at v{state['version']}")
        check(r1_uuid in state["objects"], "Agent seeded R1 to the server")

        # --- TEST 1: remote change -> KiCad -------------------------------
        print("\n--- Test 1: a remote change must move the part inside KiCad ---")
        target_x = round(by_reference["R1"]["position"]["x"] + 7.0, 3)
        target_y = by_reference["R1"]["position"]["y"]
        started = time.perf_counter()
        await peer.move(r1_uuid, "R1", target_x, target_y)
        await peer.wait_for("change_ack")

        def r1_moved():
            try:
                return probe.read_snapshot()[r1_uuid]["position"] == {"x": target_x, "y": target_y}
            except Exception:
                return False

        ok = await wait_until(r1_moved, timeout=15)
        latency = time.perf_counter() - started
        check(ok, "R1 moved inside live KiCad",
              f"to ({target_x}, {target_y}) in {latency:.2f}s")

        # --- TEST 2: no echo loop -----------------------------------------
        print("\n--- Test 2: the applied change must not bounce back ---")
        echoes = 0
        try:
            while True:
                message = await peer.wait_for("remote_change", timeout=4.0)
                for change in message.get("changes", []):
                    if change.get("uuid") == r1_uuid:
                        echoes += 1
        except (TimeoutError, asyncio.TimeoutError):
            pass
        check(echoes == 0, "No echo of the peer's own change", f"{echoes} echo(es) seen")

        # --- TEST 3: local KiCad edit -> peer ------------------------------
        print("\n--- Test 3: an edit made in KiCad must reach the other client ---")
        c1_before = probe.read_snapshot()[c1_uuid]["position"]
        c1_target = {"x": round(c1_before["x"] + 4.0, 3), "y": c1_before["y"]}

        # Move C1 through a separate IPC connection. To the agent this is
        # indistinguishable from the user dragging it in the GUI.
        started = time.perf_counter()
        probe.apply_changes([{
            "operation": "modify", "uuid": c1_uuid, "reference": "C1",
            "field": "position", "new": c1_target}], "simulated user drag")

        try:
            message = await peer.wait_for(
                "remote_change", timeout=20,
                predicate=lambda m: any(c.get("uuid") == c1_uuid for c in m.get("changes", [])))
            latency = time.perf_counter() - started
            change = next(c for c in message["changes"] if c["uuid"] == c1_uuid)
            check(change["new"] == c1_target,
                  "Peer received the KiCad-side move of C1",
                  f"{change['new']} in {latency:.2f}s (origin: {message.get('origin_user_name')})")
        except (TimeoutError, asyncio.TimeoutError):
            check(False, "Peer received the KiCad-side move of C1", "timed out after 20s")

        # --- TEST 4: stability --------------------------------------------
        print("\n--- Test 4: the system must settle, not oscillate ---")
        extra = 0
        try:
            while True:
                message = await peer.wait_for("remote_change", timeout=5.0)
                extra += len(message.get("changes", []))
        except (TimeoutError, asyncio.TimeoutError):
            pass
        check(extra == 0, "No spurious traffic once idle", f"{extra} extra change(s)")

        check(agent.poll() is None, "Agent still alive at the end")

    finally:
        await peer.close()
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()
        agent_log.close()

    print()
    failed = [label for ok, label in RESULTS if not ok]
    if failed:
        print(f"RESULT: {len(failed)}/{len(RESULTS)} checks FAILED")
        for label in failed:
            print(f"  - {label}")
        print(f"\nAgent log: {log_path}")
        return 1
    print(f"RESULT: all {len(RESULTS)} checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
