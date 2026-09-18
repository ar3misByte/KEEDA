"""Milestone 5 - soft locking, verified against a REAL running KiCad.

Proves the claim that matters: when someone else holds a lock, your edit is
refused AND your board is put back, so the two KiCad instances do not drift.

Preconditions (same as test_live_sync.py):
  1. KiCad open with sample_project/demo_board.kicad_pcb, API enabled.
  2. The sync server running:  python -m server.main

    python tests/manual/test_live_locks.py
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

SERVER = os.environ.get("KICADLIVE_SERVER", "127.0.0.1")
PORT = int(os.environ.get("KICADLIVE_PORT", "8000"))
PROJECT = "demo_board"

RESULTS: list[tuple[bool, str]] = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label))
    print(f"{'[PASS]' if ok else '[FAIL]'} {label}{(' - ' + str(detail)) if detail else ''}",
          flush=True)
    return ok


class Peer:
    """Designer B - holds the lock. No KiCad of their own."""

    def __init__(self):
        self.client_id = "locktest-peer"
        # Unique per run, so the server never mistakes a new change for a replay.
        self.run_token = uuidlib.uuid4().hex[:6]
        self.ws = None

    async def connect(self):
        self.ws = await websockets.connect(f"ws://{SERVER}:{PORT}/ws", open_timeout=10)
        await self.ws.send(json.dumps({"type": "hello", "client_id": self.client_id,
                                       "user_name": "Designer B", "project_id": PROJECT}))
        return await self.wait_for("project_state", timeout=10)

    async def wait_for(self, mtype, timeout=10.0, predicate=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"never received {mtype}")
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            if message.get("type") == mtype and (predicate is None or predicate(message)):
                return message

    async def lock(self, uuid, reference):
        await self.ws.send(json.dumps({"type": "lock_request", "client_id": self.client_id,
                                       "uuid": uuid, "reference": reference}))
        return await self.wait_for("lock_granted", timeout=10)

    async def unlock(self, uuid):
        await self.ws.send(json.dumps({"type": "lock_release", "client_id": self.client_id,
                                       "uuid": uuid}))

    async def drain(self, seconds=1.0):
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), timeout=seconds)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    async def close(self):
        if self.ws:
            await self.ws.close()


async def wait_until(predicate, timeout=15.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def main() -> int:
    print("=" * 62)
    print(" KiCad Live - live soft-lock test")
    print("=" * 62)

    probe = KiCadLink(client_name_prefix="kl-locktest")
    try:
        check(True, "KiCad reachable", probe.connect())
    except KiCadUnavailable as exc:
        check(False, "KiCad reachable", exc)
        return 1

    snapshot = probe.read_snapshot()
    by_reference = {state["reference"]: state for state in snapshot.values()}
    if not check("R2" in by_reference, "Demo board has R2"):
        return 1
    r2_uuid = by_reference["R2"]["uuid"]

    log_path = os.path.join(ROOT, "data", "locktest-agent.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    agent_log = open(log_path, "w", encoding="utf-8")
    agent = subprocess.Popen(
        [sys.executable, "-m", "agent.main", "--server", SERVER, "--port", str(PORT),
         "--name", "Designer A", "--project", PROJECT, "--client-id", "locktest-agent"],
        cwd=ROOT, stdout=agent_log, stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONPATH=ROOT), text=True)

    peer = Peer()
    try:
        await asyncio.sleep(6)
        if agent.poll() is not None:
            agent_log.flush()
            check(False, "Agent started",
                  open(log_path, encoding="utf-8", errors="replace").read()[-1200:])
            return 1
        check(True, "Agent started")

        await peer.connect()

        # ---- Designer B takes the lock on R2 -----------------------------
        granted = await peer.lock(r2_uuid, "R2")
        check(granted.get("owner") == peer.client_id, "Designer B holds the lock on R2")
        await asyncio.sleep(1.5)        # let the agent receive lock_update

        # ---- Designer A tries to move it ---------------------------------
        print("\n--- Designer A edits a component locked by Designer B ---")
        original = probe.read_snapshot()[r2_uuid]["position"]
        attempted = {"x": round(original["x"] + 6.0, 3), "y": original["y"]}

        probe.apply_changes([{"operation": "modify", "uuid": r2_uuid, "reference": "R2",
                              "field": "position", "new": attempted}],
                            "simulated drag by Designer A")
        check(probe.read_snapshot()[r2_uuid]["position"] == attempted,
              "R2 was moved locally first", str(attempted))

        # The agent must notice, refuse to share it, and put R2 back.
        def reverted():
            try:
                return probe.read_snapshot()[r2_uuid]["position"] == original
            except Exception:
                return False

        check(await wait_until(reverted, timeout=15),
              "Agent reverted the locked component", f"back to {original}")

        # Designer B must not have received the blocked change.
        got_change = True
        try:
            await peer.wait_for(
                "remote_change", timeout=4.0,
                predicate=lambda m: any(c.get("uuid") == r2_uuid for c in m.get("changes", [])))
        except (TimeoutError, asyncio.TimeoutError):
            got_change = False
        check(not got_change, "Locked change was never broadcast")

        # ---- Designer B releases, Designer A tries again -------------------
        print("\n--- Designer B releases the lock ---")
        await peer.unlock(r2_uuid)
        await peer.wait_for("lock_update", timeout=10,
                            predicate=lambda m: not any(
                                lock["uuid"] == r2_uuid for lock in m.get("locks", [])))
        check(True, "Lock released")
        await asyncio.sleep(1.5)

        await peer.drain(0.5)
        allowed = {"x": round(original["x"] + 3.0, 3), "y": original["y"]}
        probe.apply_changes([{"operation": "modify", "uuid": r2_uuid, "reference": "R2",
                              "field": "position", "new": allowed}],
                            "simulated drag after release")
        try:
            message = await peer.wait_for(
                "remote_change", timeout=20,
                predicate=lambda m: any(c.get("uuid") == r2_uuid for c in m.get("changes", [])))
            change = next(c for c in message["changes"] if c["uuid"] == r2_uuid)
            check(change["new"] == allowed, "Change accepted once unlocked", str(change["new"]))
        except (TimeoutError, asyncio.TimeoutError):
            check(False, "Change accepted once unlocked", "timed out after 20s")

        # Put the board back the way we found it.
        probe.apply_changes([{"operation": "modify", "uuid": r2_uuid, "reference": "R2",
                              "field": "position", "new": original}], "restore")
        await asyncio.sleep(1.0)

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
