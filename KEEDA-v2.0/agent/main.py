"""KiCad Live Agent - runs on each designer's computer.

    python -m agent.main --server 192.168.1.50 --name "Designer A"

Two ways to collaborate, chosen automatically:

  * LIVE mode   - KiCad's IPC API is usable: PCB edits sync per move.
  * FILE-ONLY   - the API is busy / timing out / unavailable (or --file-only):
                  schematic and PCB FILES are shared and merged, and the KiCad
                  editors are reloaded for you (see agent/schematic_sync.py).

Schematic sharing (files + merge + auto save/reload) runs in both modes.
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

from agent import win_ui
from agent.kicad_link import KiCadBusy, KiCadLink, KiCadUnavailable
from agent.schematic_link import SchematicLink, SchematicUnavailable
from agent.schematic_sync import SchematicSync
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


def find_project_files(project_dir: str) -> tuple[str | None, str]:
    """(project id, file name) taken from the KiCad files in a folder."""
    try:
        names = sorted(os.listdir(project_dir))
    except OSError:
        return None, f"cannot read {project_dir}"
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_sch"):
        for name in names:
            if name.endswith(suffix) and not name.startswith((".", "~", "_autosave")):
                return project_id_from_board(name), name
    return None, f"no KiCad files in {project_dir}"


async def connect_live(link: KiCadLink) -> str:
    """Attach to KiCad's API, waiting briefly while it is busy loading."""
    for attempt in range(15):
        try:
            return link.connect()
        except KiCadBusy:
            if attempt == 0:
                print(" KiCad is busy - waiting for it (close any open dialog)...")
            await asyncio.sleep(2.0)
    raise KiCadUnavailable("KiCad kept answering 'busy' or timing out for 30 s")


async def run(args) -> int:
    print("=" * 62)
    print(f" KiCad Live Agent {AGENT_VERSION}")
    print("=" * 62)

    link = KiCadLink()
    file_only = args.file_only
    board_name = ""
    if not file_only:
        try:
            board_name = await connect_live(link)
        except KiCadUnavailable as exc:
            print(f"\n [!] KiCad's live API is not usable on this computer:\n     {exc}")
            print("     Switching to FILE-ONLY mode: schematic and PCB files are shared and")
            print("     merged and the KiCad editors reload by themselves. Layout and")
            print("     schematic sync still work; they update when a file is saved.\n")
            file_only = True

    if file_only:
        project_dir = os.path.abspath(args.project_dir or os.getcwd())
        found_id, note = find_project_files(project_dir)
        if found_id is None:
            print(f"\n[FAIL] {note}.\n       Start the agent inside your KiCad project folder, "
                  'or pass --project-dir "C:\\path\\to\\project".')
            return 2
        project_id = args.project or found_id
        board_name = note
    else:
        project_dir = os.path.abspath(args.project_dir or link.project_dir or os.getcwd())
        project_id = args.project or project_id_from_board(board_name)
    client_id = args.client_id or stable_client_id()

    schematic = None
    schematic_status = "disabled"
    if not args.no_schematic:
        try:
            sch_link = SchematicLink(project_dir)
            sheet_name = sch_link.connect()
            schematic = sch_link
            stats = sch_link.stats()
            schematic_status = (f"{sheet_name} ({stats['objects']} objects, "
                                f"{stats['sheets']} sheet(s))")
        except SchematicUnavailable as exc:
            schematic_status = f"not found ({exc})"
        except Exception as exc:
            schematic_status = f"unavailable ({exc})"

    mode = "FILE-ONLY (no KiCad API)" if file_only else "LIVE KiCad API + file sharing"
    print(f" Mode       {mode}")
    print(f" KiCad      {'n/a' if file_only else link.version()}")
    print(f" Board      {board_name}")
    print(f" Project    {project_id}  (must be identical on every computer)")
    print(f" Folder     {project_dir}")
    print(f" User       {args.name}")
    print(f" Client id  {client_id}")
    print(f" Schematic  {schematic_status}")
    print(f" Server     ws://{args.server}:{args.port}/ws")
    if args.read_only:
        print(" Read-only  receives changes, sends none")

    ws = WSClient(args.server, args.port, client_id, args.name, project_id)
    if file_only:
        from agent.file_only import FileOnlyAgent, NullLink
        agent = FileOnlyAgent(NullLink(), ws, args.name, read_only=args.read_only,
                              poll_interval=args.poll_interval, schematic=schematic)
    else:
        agent = SyncAgent(link, ws, args.name, read_only=args.read_only,
                          poll_interval=args.poll_interval, schematic=schematic)

    if args.schematic_sync_interval > 0:
        agent.schematic_sync = SchematicSync(
            project_dir, ws, args.name, read_only=args.read_only,
            interval=args.schematic_sync_interval, banner=agent.banner,
            on_applied=lambda names: schematic.resync() if schematic is not None else None,
            sync_pcb=file_only or args.sync_pcb_files,
            auto_save=not args.no_auto_save, auto_reload=not args.no_auto_reload,
            autosave_delay=args.autosave_delay)
        shared = "schematic + PCB" if agent.schematic_sync.sync_pcb else "schematic"
        auto = win_ui.AVAILABLE
        print(f" File share {shared} files | auto-save "
              f"{'on' if auto and not args.no_auto_save else 'off'} | auto-reload "
              f"{'on' if auto and not args.no_auto_reload else 'off'}")
    print("=" * 62)
    print(" Press Ctrl+C to stop.\n")

    async def on_connect():
        # A fresh connection means our view may be stale; ask for the truth.
        agent.on_reconnect()
        await agent.send_presence("viewing", kicad_connected=not file_only)

    ws.on_connect = on_connect

    tasks = [asyncio.create_task(ws.run()), asyncio.create_task(agent.run())]
    if agent.schematic_sync is not None:
        tasks.append(asyncio.create_task(agent.schematic_sync.run()))
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
        if agent.schematic_sync is not None:
            print(f" File sharing: {agent.schematic_sync.stats}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KiCad Live Agent")
    parser.add_argument("--server", required=True, help="sync server IP or hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--name", required=True, help="your display name, e.g. \"Designer A\"")
    parser.add_argument("--project", default=None,
                        help="project id (default: derived from the board/project file name)")
    parser.add_argument("--client-id", default=None, help="override the stored client id")
    parser.add_argument("--read-only", action="store_true",
                        help="receive changes but never send any")
    parser.add_argument("--project-dir", default=None,
                        help="folder holding the .kicad_sch/.kicad_pcb "
                             "(default: asked from KiCad, else the current directory)")
    parser.add_argument("--no-schematic", action="store_true",
                        help="do not watch the schematic at all")
    parser.add_argument("--schematic-sync-interval", type=float, default=30.0,
                        help="fallback seconds between file refreshes (default 30; new "
                             "saves are shared immediately; 0 disables file sharing)")
    parser.add_argument("--file-only", action="store_true",
                        help="do not use KiCad's IPC API at all: share and merge the "
                             "schematic and PCB files instead (chosen automatically when "
                             "the API is busy or timing out)")
    parser.add_argument("--sync-pcb-files", action="store_true",
                        help="also share the .kicad_pcb file (implied by --file-only)")
    parser.add_argument("--no-auto-save", action="store_true",
                        help="do not save the editor for you; edits are shared only "
                             "when you press Ctrl+S")
    parser.add_argument("--no-auto-reload", action="store_true",
                        help="do not reload the editor for you (do File > Revert yourself)")
    parser.add_argument("--autosave-delay", type=float, default=3.0,
                        help="seconds an edit stays unsaved before auto-save (default 3)")
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
