"""KiCad Live Agent - runs on each designer's computer.

Connects a running KiCad to the sync server. KiCad must already be open with a
board, and 'Enable KiCad API' must be ticked in Preferences > Plugins.

    python -m agent.main --server 192.168.1.50 --name "Designer A"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid as uuidlib

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.kicad_link import KiCadBusy, KiCadLink, KiCadUnavailable
from agent.schematic_link import SchematicLink, SchematicUnavailable
from agent.sync_agent import SyncAgent
from agent.ws_client import WSClient
from common.protocol import AGENT_VERSION, DEFAULT_PORT, POLL_INTERVAL

log = logging.getLogger("kicadlive.agent")

CLIENT_ID_FILE = os.path.join(
    os.path.expanduser("~"), ".kicad_live_client_id")


def stable_client_id() -> str:
    """A client id that survives restarts, so reconnects are recognised."""
    try:
        if os.path.exists(CLIENT_ID_FILE):
            with open(CLIENT_ID_FILE, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value[:64]
    except OSError:
        pass
    value = uuidlib.uuid4().hex[:12]
    try:
        with open(CLIENT_ID_FILE, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError:
        pass          # a per-run id still works, it just looks like a new client
    return value


def project_id_from_board(board_name: str) -> str:
    """Turn 'demo_board.kicad_pcb' into 'demo_board'."""
    base = os.path.basename(board_name or "")
    for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in base)
    return cleaned[:64] or "default"


async def run(args) -> int:
    print("=" * 62)
    print(f" KiCad Live Agent {AGENT_VERSION}")
    print("=" * 62)

    link = KiCadLink()
    try:
        # KiCad answers "busy" while it is still loading the board or while a
        # dialog is open. That is not a failure: wait for it instead of quitting.
        for attempt in range(30):
            try:
                board_name = link.connect()
                break
            except KiCadBusy:
                if attempt == 0:
                    print(" KiCad is busy - waiting for it (close any open dialog)...")
                await asyncio.sleep(2.0)
        else:
            raise KiCadUnavailable("KiCad stayed busy for 60 s. Close any open dialog "
                                   "in KiCad and start the agent again.")
    except KiCadUnavailable as exc:
        print("\n[FAIL] Could not connect to KiCad.")
        print(f"       {exc}\n")
        print("  Checklist:")
        print("   1. Is KiCad open with a PCB (pcbnew) window?")
        print("   2. Preferences > Plugins > 'Enable KiCad API' ticked?")
        print("   3. Did you restart KiCad after ticking it?")
        print("\n  See docs/TROUBLESHOOTING.md -> 'Agent cannot connect to KiCad'.")
        return 2

    project_id = args.project or project_id_from_board(board_name)
    client_id = args.client_id or stable_client_id()

    # Schematic collaboration is file-based and read-only (eeschema has no IPC
    # API in KiCad 10). It needs the project directory; default to the working
    # directory, which is the project folder in the documented workflow.
    schematic = None
    schematic_status = "disabled"
    if not args.no_schematic:
        project_dir = os.path.abspath(args.project_dir or os.getcwd())
        try:
            sch_link = SchematicLink(project_dir)
            sheet_name = sch_link.connect()
            schematic = sch_link
            stats = sch_link.stats()
            schematic_status = (f"{sheet_name} ({stats['objects']} objects, "
                                f"{stats['sheets']} sheet(s)) - review only")
        except SchematicUnavailable as exc:
            schematic_status = f"not found ({exc})"
        except Exception as exc:
            schematic_status = f"unavailable ({exc})"

    print(f" KiCad      {link.version()}")
    print(f" Board      {board_name}")
    print(f" Project    {project_id}")
    print(f" User       {args.name}")
    print(f" Client id  {client_id}")
    print(f" Schematic  {schematic_status}")
    print(f" Server     ws://{args.server}:{args.port}/ws")
    if args.read_only:
        print(" Mode       READ-ONLY (receives changes, sends none)")
    print("=" * 62)
    print(" Press Ctrl+C to stop.\n")

    ws = WSClient(args.server, args.port, client_id, args.name, project_id)
    agent = SyncAgent(link, ws, args.name, read_only=args.read_only,
                      poll_interval=args.poll_interval, schematic=schematic)

    async def on_connect():
        # A fresh connection means our view may be stale; ask for the truth.
        agent.on_reconnect()
        await agent.send_presence("viewing")

    ws.on_connect = on_connect

    tasks = [asyncio.create_task(ws.run()), asyncio.create_task(agent.run())]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await ws.stop()
        print(f"\n Session totals: pcb-sent={agent.stats['sent']} "
              f"schematic-sent={agent.stats['schematic_sent']} "
              f"received={agent.stats['received']} "
              f"conflicts={agent.stats['conflicts']} "
              f"blocked-by-lock={agent.stats['blocked']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KiCad Live Agent")
    parser.add_argument("--server", required=True, help="sync server IP or hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--name", required=True, help="your display name, e.g. \"Designer A\"")
    parser.add_argument("--project", default=None,
                        help="project id (default: derived from the open board's filename)")
    parser.add_argument("--client-id", default=None, help="override the stored client id")
    parser.add_argument("--read-only", action="store_true",
                        help="receive changes but never send any")
    parser.add_argument("--project-dir", default=None,
                        help="project folder holding the .kicad_sch "
                             "(default: the current directory)")
    parser.add_argument("--no-schematic", action="store_true",
                        help="do not watch the schematic at all")
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL,
                        help=f"board poll period in seconds (default {POLL_INTERVAL})")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
