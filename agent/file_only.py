"""File-only agent: collaboration without KiCad's IPC API.

Used when KiCad's API is unusable on a computer (it answers "busy" or times
out for ever), or when asked for with --file-only. Nothing is read from or
written to a running KiCad through the API. Instead the schematic and PCB
FILES are shared and merged (agent/schematic_sync.py) and the editors are
reloaded through their File > Revert menu item (agent/win_ui.py).
"""
from __future__ import annotations

import logging

from agent.sync_agent import SyncAgent

log = logging.getLogger("kicadlive.agent")


class NullLink:
    """Stands in for KiCadLink: there is no live board."""

    connected = False

    def version(self) -> str:
        return "not used (file-only mode)"


class FileOnlyAgent(SyncAgent):
    """Keeps presence, comments and schematic activity; skips the live board."""

    async def _step(self) -> None:
        await self.poll_schematic()

    async def _on_project_state(self, message: dict) -> None:
        self.locks = {lock["uuid"]: lock for lock in message.get("locks", [])}
        self.state_ready.set()
        await self.send_presence("viewing", kicad_connected=False)

    async def _on_remote_change(self, message: dict) -> None:
        # PCB objects arrive as FILES in this mode; only note schematic reports.
        for change in (message.get("changes") or [])[:6]:
            if change.get("domain") == "schematic":
                log.info("SCHEM  %s changed %s (%s)", message.get("origin_user_name", "someone"),
                         change.get("reference") or change.get("uuid", "")[:8],
                         change.get("field") or change.get("operation"))
