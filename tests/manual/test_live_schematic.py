"""Live schematic collaboration test.

Proves the schematic half of the system end to end:

    someone edits and saves .kicad_sch
              |
        agent detects it
              |
          sync server
              |
      other designers + dashboard

Schematic changes are REPORTED, not applied - eeschema exposes no IPC API in
KiCad 10, so nothing is ever written into anyone's schematic editor. This test
therefore checks that an edit is DETECTED and BROADCAST, not that it appears in
a second eeschema.

Preconditions:
  1. KiCad open with sample_project/demo_board.kicad_pcb (for the PCB link).
  2. The sync server running:  python -m server.main

    python tests/manual/test_live_schematic.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import time
import uuid as uuidlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import websockets

SERVER = os.environ.get("KICADLIVE_SERVER", "127.0.0.1")
PORT = int(os.environ.get("KICADLIVE_PORT", "8000"))
PROJECT = "demo_board"
SCH = os.path.join(ROOT, "sample_project", "demo_board.kicad_sch")

RESULTS: list[tuple[bool, str]] = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label))
    print(f"{'[PASS]' if ok else '[FAIL]'} {label}{(' - ' + str(detail)) if detail else ''}",
          flush=True)
    return ok


class Peer:
    """Designer B, watching from the dashboard side."""

    def __init__(self):
        self.client_id = "schtest-peer"
        self.run_token = uuidlib.uuid4().hex[:6]
        self.ws = None

    async def connect(self):
        self.ws = await websockets.connect(f"ws://{SERVER}:{PORT}/ws", open_timeout=10)
        await self.ws.send(json.dumps({"type": "hello", "client_id": self.client_id,
                                       "user_name": "Designer B", "project_id": PROJECT}))
        return await self.wait_for("project_state")

    async def wait_for(self, mtype, timeout=25.0, predicate=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"never received {mtype}")
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            if msg.get("type") == mtype and (predicate is None or predicate(msg)):
                return msg

    async def close(self):
        if self.ws:
            await self.ws.close()


def edit_schematic(old_value: str, new_value: str) -> bool:
    """Simulate a designer changing R1's value and pressing Ctrl+S."""
    text = io.open(SCH, encoding="utf-8").read()
    needle = f'(property "Value" "{old_value}"'
    if needle not in text:
        return False
    text = text.replace(needle, f'(property "Value" "{new_value}"', 1)
    io.open(SCH, "w", encoding="utf-8").write(text)
    return True


async def main() -> int:
    print("=" * 62)
    print(" KiCad Live - live schematic collaboration test")
    print("=" * 62)

    if not check(os.path.exists(SCH), "Sample schematic exists", SCH):
        print("   Run: python tools/make_sample_schematic.py")
        return 1

    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{SERVER}:{PORT}/health", timeout=5) as response:
            check(json.loads(response.read())["status"] == "ok", "Sync server reachable")
    except Exception as exc:
        check(False, "Sync server reachable", exc)
        return 1

    backup = SCH + ".testbackup"
    shutil.copyfile(SCH, backup)

    log_path = os.path.join(ROOT, "data", "schtest-agent.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    agent_log = open(log_path, "w", encoding="utf-8")
    agent = subprocess.Popen(
        [sys.executable, "-m", "agent.main", "--server", SERVER, "--port", str(PORT),
         "--name", "Designer A", "--project", PROJECT, "--client-id", "schtest-agent",
         "--project-dir", os.path.join(ROOT, "sample_project")],
        cwd=ROOT, stdout=agent_log, stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONPATH=ROOT), text=True)

    peer = Peer()
    try:
        await asyncio.sleep(7)
        if agent.poll() is not None:
            agent_log.flush()
            check(False, "Agent started",
                  io.open(log_path, encoding="utf-8", errors="replace").read()[-1200:])
            return 1
        check(True, "Agent started")

        # Assert on the logger line, not the startup banner: `print()` output
        # is block-buffered in a redirected subprocess and may not be on disk
        # yet, which would make this check flaky rather than meaningful.
        agent_text = io.open(log_path, encoding="utf-8", errors="replace").read()
        watching = next((line for line in agent_text.splitlines()
                         if "watching schematic" in line), "")
        check(bool(watching), "Agent is watching the schematic",
              watching.split("] ", 1)[-1] if watching else "no 'watching schematic' log line")

        await peer.connect()
        check(True, "Peer connected")

        # --- the edit -----------------------------------------------------
        print("\n--- Designer A edits R1's value and saves ---")
        if not check(edit_schematic("4k7", "10k"), "Schematic edited on disk",
                     "R1: 4k7 -> 10k"):
            return 1

        started = time.perf_counter()
        try:
            message = await peer.wait_for(
                "remote_change", timeout=25,
                predicate=lambda m: any(c.get("domain") == "schematic"
                                        and c.get("reference") == "R1"
                                        for c in m.get("changes", [])))
            latency = time.perf_counter() - started
            change = next(c for c in message["changes"] if c.get("reference") == "R1")
            check(change["new"] == "10k",
                  "Schematic change reached the other client",
                  f"{change['field']} -> {change['new']} in {latency:.2f}s")
            check(change["domain"] == "schematic", "Change is tagged as schematic")
        except (TimeoutError, asyncio.TimeoutError):
            check(False, "Schematic change reached the other client", "timed out")

        # --- it must appear in the activity feed ---------------------------
        await asyncio.sleep(0.6)
        import urllib.request
        with urllib.request.urlopen(
                f"http://{SERVER}:{PORT}/api/activity?project={PROJECT}&domain=schematic",
                timeout=10) as response:
            events = json.loads(response.read())["events"]
        entry = next((e for e in events if e.get("object_ref") == "R1"), None)
        check(entry is not None, "Recorded in the activity timeline",
              entry["description"] if entry else "not found")
        if entry:
            check(entry["username"] == "Designer A", "Attributed to the right user")
            check("10k" in (entry.get("description") or ""),
                  "Description names the new value", entry["description"])

        # --- the schematic file must be untouched by us --------------------
        print("\n--- KiCad Live must never write the schematic ---")
        current = io.open(SCH, encoding="utf-8").read()
        check('(property "Value" "10k"' in current,
              "Schematic still holds the author's edit",
              "KiCad Live did not overwrite it")

        # --- a second edit must also be seen -------------------------------
        print("\n--- A second edit must also be detected ---")
        edit_schematic("10k", "2k2")
        try:
            message = await peer.wait_for(
                "remote_change", timeout=25,
                predicate=lambda m: any(c.get("new") == "2k2" for c in m.get("changes", [])))
            check(True, "Second edit detected", "R1 -> 2k2")
        except (TimeoutError, asyncio.TimeoutError):
            check(False, "Second edit detected", "timed out")

        check(agent.poll() is None, "Agent still alive")

    finally:
        await peer.close()
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()
        agent_log.close()
        # Always put the sample project back the way we found it.
        shutil.move(backup, SCH)
        print("\n[info] sample schematic restored")

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
