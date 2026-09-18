"""The synchronisation loop.

Poll the live board, work out what the user changed, send it; receive other
people's changes and apply them. Everything that keeps the two sides from
fighting each other lives here.
"""
from __future__ import annotations

import asyncio
import logging
import uuid as uuidlib

from common.diff_engine import apply_change, diff_snapshots, summarise
from common.protocol import POLL_INTERVAL, SYNCED_FIELDS
from agent.kicad_link import KiCadBusy, KiCadLink, KiCadUnavailable
from agent.schematic_link import SchematicLink, SchematicUnavailable
from agent.ws_client import WSClient

log = logging.getLogger("kicadlive.agent")

SELECTION_EVERY = 2          # poll selection every Nth board poll
SCHEMATIC_EVERY = 4          # check schematic files every Nth poll (~1 s)


class SyncAgent:
    def __init__(self, link: KiCadLink, ws: WSClient, user_name: str,
                 read_only: bool = False, poll_interval: float = POLL_INTERVAL,
                 schematic: SchematicLink | None = None):
        self.link = link
        self.schematic = schematic
        self.ws = ws
        self.user_name = user_name
        self.read_only = read_only
        self.poll_interval = poll_interval

        # What we believe is in sync with the server.
        self.baseline: dict[str, dict] = {}
        # Per-field versions, mirroring the server's model: (uuid, field) -> version
        self.field_versions: dict[tuple[str, str], int] = {}

        # UUIDs whose next detected change may be our own write coming back,
        # mapped to the poll tick on which they were marked. Marks EXPIRE after
        # one full poll cycle - see _expire_echo_marks for why that matters.
        self.echo_marks: dict[str, int] = {}

        self.locks: dict[str, dict] = {}        # uuid -> lock record from the server
        self.my_locks: set[str] = set()
        self.selection: dict[str, str] = {}     # uuid -> reference
        self.last_activity = "idle"

        self.state_ready = asyncio.Event()
        self.change_counter = 0
        self.run_token = uuidlib.uuid4().hex[:6]
        self.stats = {"sent": 0, "received": 0, "conflicts": 0, "blocked": 0,
                      "schematic_sent": 0}
        self._tick = 0

        # Server state received but not yet adopted (KiCad was busy). The poll
        # loop keeps retrying it; giving up would leave this machine never
        # syncing at all.
        self._pending_state: dict | None = None
        # Remote changes KiCad could not accept yet, kept IN ORDER.
        self._deferred: list[tuple[list[dict], str]] = []

    # ------------------------------------------------------------------ utils

    def _next_change_id(self) -> str:
        """A change id unique across agent RESTARTS, not just within one run.

        The server de-duplicates by change_id so a retried message is never
        applied twice. Without the per-run token, an agent that restarted would
        begin counting from 1 again, and the server would silently discard its
        first changes as replays of the previous session.
        """
        self.change_counter += 1
        return f"{self.ws.client_id}-{self.run_token}-{self.change_counter}"

    def _version_of(self, uuid: str, field: str) -> int:
        return self.field_versions.get((uuid, field), 0)

    def _blocking_lock(self, uuid: str) -> dict | None:
        lock = self.locks.get(uuid)
        if lock is not None and lock.get("owner") != self.ws.client_id:
            return lock
        return None

    def banner(self, text: str) -> None:
        """User-facing notice. The agent console is part of the UI."""
        print(f"\n  >>> {text}\n", flush=True)

    # ------------------------------------------------------------- main loops

    async def run(self) -> None:
        await asyncio.gather(self.inbound_loop(), self.poll_loop())

    async def inbound_loop(self) -> None:
        while True:
            message = await self.ws.incoming.get()
            try:
                await self.handle(message)
            except Exception:
                log.exception("failed to handle %s", message.get("type"))

    def on_reconnect(self) -> None:
        """A fresh server connection brings a fresh project_state; drop stale work."""
        self.state_ready.clear()
        self._pending_state = None
        self._deferred.clear()

    async def _step(self) -> None:
        """One unit of work. Raises KiCadBusy / KiCadUnavailable on KiCad trouble."""
        if self._pending_state is not None:
            await self._sync_state(self._pending_state)
        elif self.state_ready.is_set():
            await self._flush_deferred()
            await self.poll_once()

    async def poll_loop(self) -> None:
        delay = self.poll_interval
        busy_streak = 0
        while True:
            await asyncio.sleep(delay)
            if not self.ws.connected.is_set():
                continue
            try:
                await self._step()
            except KiCadBusy:
                # Transient: a dialog is open, or the user is mid-drag. Back off
                # gently and try again. Nothing is torn down and nobody is told
                # this user went offline.
                busy_streak += 1
                if busy_streak == 1:
                    log.warning("KiCad is busy (open dialog or an operation in "
                                "progress) - waiting; nothing is lost")
                delay = min(self.poll_interval * (2 ** min(busy_streak, 4)), 3.0)
            except KiCadUnavailable as exc:
                log.warning("KiCad unavailable: %s", exc)
                await self._recover_kicad()
                delay = self.poll_interval
            except Exception:
                log.exception("poll failed")
                await asyncio.sleep(1.0)
                delay = self.poll_interval
            else:
                if busy_streak:
                    log.info("KiCad is responding again")
                busy_streak = 0
                delay = self.poll_interval

    async def _recover_kicad(self) -> None:
        """KiCad is genuinely gone (closed/restarted). Never let this raise:
        an exception escaping the poll loop would kill the whole agent."""
        try:
            await self.send_presence("offline", kicad_connected=False)
            self.link.connect()
            self.baseline = self.link.read_snapshot()
        except KiCadUnavailable:
            await asyncio.sleep(2.0)
            return
        except Exception:
            log.exception("could not recover the KiCad connection")
            await asyncio.sleep(2.0)
            return
        log.info("reconnected to KiCad")
        self._deferred.clear()
        await self.request_state()
        await self.send_presence(self.last_activity)

    # ------------------------------------------------------------ local edits

    async def poll_once(self) -> None:
        self._tick += 1

        current = self.link.read_snapshot()
        changes = diff_snapshots(self.baseline, current)

        if changes:
            await self.process_local_changes(changes, current)

        self._expire_echo_marks()

        if self._tick % SELECTION_EVERY == 0:
            await self.poll_selection()

        if self.schematic is not None and self._tick % SCHEMATIC_EVERY == 0:
            await self.poll_schematic()

    def _expire_echo_marks(self) -> None:
        """Drop echo marks that have already survived a full poll cycle.

        Echo marks must expire. The real loop-breaker is the baseline update in
        `apply_remote`; a mark is only a backstop for the case where KiCad
        rounds a coordinate so the write-back looks like a tiny edit. If marks
        lived for ever, the first GENUINE edit the user later made to that same
        component would be silently swallowed as an 'echo' - which is exactly
        what the live lock test caught.
        """
        if not self.echo_marks:
            return
        self.echo_marks = {uuid: tick for uuid, tick in self.echo_marks.items()
                           if self._tick - tick < 2}

    async def process_local_changes(self, changes: list[dict], current: dict[str, dict]) -> None:
        outgoing: list[dict] = []
        revert: list[dict] = []

        for change in changes:
            uuid = change.get("uuid")
            if not uuid:
                continue

            # 1. Echo suppression. This is what stops A -> B -> A for ever.
            if uuid in self.echo_marks:
                del self.echo_marks[uuid]
                apply_change(self.baseline, change)
                log.debug("suppressed echo for %s", change.get("reference") or uuid[:8])
                continue

            # 2. Lock enforcement. Someone else owns this part, so put it back.
            lock = self._blocking_lock(uuid)
            if lock is not None:
                reference = change.get("reference") or uuid[:8]
                self.stats["blocked"] += 1
                self.banner(f"{reference} is currently locked by {lock.get('owner_name')} "
                            f"- your change was reverted.")
                baseline_state = self.baseline.get(uuid)
                if baseline_state is not None and change.get("operation") == "modify":
                    field = change["field"]
                    revert.append({
                        "operation": "modify", "object_type": "footprint", "uuid": uuid,
                        "reference": reference, "field": field,
                        "new": baseline_state.get(field),
                    })
                continue

            # 3. Read-only mode observes without contributing.
            if self.read_only:
                apply_change(self.baseline, change)
                continue

            if change.get("operation") == "modify":
                change["base_version"] = self._version_of(uuid, change["field"])
            else:
                change["base_version"] = 0
            outgoing.append(change)

        if revert:
            await self.apply_remote(revert, "KiCad Live: reverted (locked)")

        if not outgoing:
            return

        # Update the baseline optimistically. If the server rejects a change we
        # put the object back from the server's own value, so we never sit in a
        # loop resending the same edit while waiting for an answer.
        for change in outgoing:
            apply_change(self.baseline, change)

        payload = {
            "type": "change",
            "client_id": self.ws.client_id,
            "change_id": self._next_change_id(),
            "changes": outgoing,
        }
        if await self.ws.send(payload):
            self.stats["sent"] += len(outgoing)
            for change in outgoing:
                log.info("LOCAL  %s", summarise(change))

    # ------------------------------------------------------------- schematic

    async def poll_schematic(self) -> None:
        """Report schematic edits after the author saves.

        Read-only by design: schematic changes are broadcast for review, never
        applied to anyone's eeschema. See agent/schematic_link.py for why.
        """
        if self.schematic is None or self.read_only:
            return
        try:
            changes = self.schematic.poll()
        except Exception:
            log.exception("schematic poll failed")
            return
        if not changes:
            return

        outgoing = []
        for change in changes:
            uuid = change.get("uuid")
            lock = self.locks.get(f"schematic:{uuid}")
            if lock is not None and lock.get("owner") != self.ws.client_id:
                # We cannot revert a schematic (no write path), so warn loudly
                # instead - the other designer owns this part.
                self.stats["blocked"] += 1
                self.banner(f"{change.get('reference') or uuid[:8]} is locked by "
                            f"{lock.get('owner_name')} in the schematic. Your edit was "
                            f"NOT shared - coordinate before saving again.")
                continue
            change["base_version"] = 0     # schematic state is report-only
            outgoing.append(change)

        if not outgoing:
            return

        await self.ws.send({
            "type": "change", "client_id": self.ws.client_id,
            "change_id": self._next_change_id(), "domain": "schematic",
            "changes": outgoing,
        })
        self.stats["schematic_sent"] += len(outgoing)
        for change in outgoing[:8]:
            log.info("SCHEM  %s", self.schematic.describe(change))
        if len(outgoing) > 8:
            log.info("SCHEM  ... and %d more", len(outgoing) - 8)

    # ------------------------------------------------------------- selection

    async def poll_selection(self) -> None:
        try:
            selection = self.link.selection_uuids()
        except Exception:
            return
        if selection == self.selection:
            return

        added = set(selection) - set(self.selection)
        removed = set(self.selection) - set(selection)
        self.selection = selection

        for uuid in removed:
            if uuid in self.my_locks:
                self.my_locks.discard(uuid)
                await self.ws.send({"type": "lock_release",
                                    "client_id": self.ws.client_id, "uuid": uuid})

        if not self.read_only:
            for uuid in added:
                await self.ws.send({"type": "lock_request", "client_id": self.ws.client_id,
                                    "uuid": uuid, "reference": selection[uuid]})

        activity = "editing" if selection else "viewing"
        await self.send_presence(activity)

    async def send_presence(self, activity: str, kicad_connected: bool = True) -> None:
        self.last_activity = activity
        await self.ws.send({
            "type": "presence", "client_id": self.ws.client_id,
            "activity": activity, "selection": list(self.selection.values()),
            "kicad_connected": kicad_connected,
        })

    async def request_state(self) -> None:
        await self.ws.send({"type": "request_state", "client_id": self.ws.client_id})

    # ------------------------------------------------------- inbound handling

    async def handle(self, message: dict) -> None:
        handler = getattr(self, f"_on_{message.get('type')}", None)
        if handler is not None:
            await handler(message)

    async def _on_welcome(self, message: dict) -> None:
        log.info("server %s accepted us; %d client(s) on '%s'",
                 message.get("server_version"), message.get("client_count"),
                 message.get("project_id"))

    async def _on_project_state(self, message: dict) -> None:
        """Remember the server's state and try to adopt it now.

        If KiCad is busy at this instant we must NOT give up. Returning here
        without setting `state_ready` used to leave the poll loop waiting for
        ever, so the machine never adopted existing work and never sent its
        own edits. The message stays pending and the poll loop retries it.
        """
        self.locks = {lock["uuid"]: lock for lock in message.get("locks", [])}
        self._pending_state = message
        try:
            await self._sync_state(message)
        except KiCadUnavailable as exc:
            log.warning("board not readable yet (%s); will keep retrying", exc)

    async def _sync_state(self, message: dict) -> None:
        """Adopt the server's state wholesale. Also the reconnect path."""
        server_objects = message.get("objects") or {}
        current = self.link.read_snapshot()     # KiCadBusy/Unavailable -> retried
        # What the board REALLY holds. Without this, an empty baseline makes the
        # next poll report every footprint as a brand-new local "add".
        self.baseline = current

        if not server_objects:
            await self.seed(current)
            self._pending_state = None
            self.state_ready.set()
            return

        # Record the server's versions.
        self.field_versions.clear()
        for uuid, obj in server_objects.items():
            for field, version in (obj.get("field_versions") or {}).items():
                self.field_versions[(uuid, field)] = int(version)

        # Bring our board into line with the server. A reconnecting client
        # ACCEPTS server state; it never pushes its stale version back.
        to_apply: list[dict] = []
        for uuid, server_object in server_objects.items():
            local = current.get(uuid)
            if local is None:
                continue
            for field in SYNCED_FIELDS:
                if field not in server_object:
                    continue
                from common.diff_engine import values_equal
                if not values_equal(field, local.get(field), server_object[field]):
                    to_apply.append({
                        "operation": "modify", "object_type": "footprint", "uuid": uuid,
                        "reference": server_object.get("reference", ""),
                        "field": field, "new": server_object[field],
                    })

        if to_apply:
            log.info("adopting %d field(s) from the server", len(to_apply))
            await self.apply_remote(to_apply, "KiCad Live: sync with server")
        else:
            self.baseline = current

        # Anything we have that the server has never seen gets contributed.
        unknown = [u for u in current if u not in server_objects]
        if unknown and not self.read_only:
            await self.seed({u: current[u] for u in unknown}, partial=True)

        self._pending_state = None
        self.state_ready.set()
        await self.send_presence(self.last_activity)

    async def seed(self, objects: dict[str, dict], partial: bool = False) -> None:
        """Contribute objects the server does not know about yet."""
        if not objects or self.read_only:
            return
        changes = [{
            "operation": "add", "object_type": "footprint", "uuid": uuid,
            "reference": state.get("reference", ""), "base_version": 0, "state": state,
        } for uuid, state in objects.items()]
        log.info("%s the project with %d footprint(s)",
                 "extending" if partial else "seeding", len(changes))
        await self.ws.send({"type": "change", "client_id": self.ws.client_id,
                            "change_id": self._next_change_id(), "changes": changes})

    async def _on_remote_change(self, message: dict) -> None:
        changes = message.get("changes") or []
        origin = message.get("origin_user_name", "someone")

        # Schematic changes are REPORTED, never applied: eeschema has no IPC
        # API, and writing the .kicad_sch under a running editor would be
        # overwritten on the author's next save. Surface them instead.
        schematic_changes = [c for c in changes if c.get("domain") == "schematic"]
        if schematic_changes:
            for change in schematic_changes[:6]:
                log.info("SCHEM  %s changed %s (%s) - review in your schematic editor",
                         origin, change.get("reference") or change.get("uuid", "")[:8],
                         change.get("field") or change.get("operation"))
            self.stats["received"] += len(schematic_changes)
            changes = [c for c in changes if c.get("domain") != "schematic"]
            if not changes:
                return
        for change in changes:
            for field in ("field",):
                if change.get(field):
                    self.field_versions[(change["uuid"], change[field])] = int(change.get("version", 0))
        applicable = [c for c in changes if c.get("operation") == "modify"]
        if not applicable:
            # An `add` from another client: record it so we do not re-contribute it.
            for change in changes:
                apply_change(self.baseline, change)
            return
        for change in applicable:
            log.info("REMOTE %s (%s)", summarise(change), origin)
        self.stats["received"] += len(applicable)
        await self.apply_remote(applicable, f"KiCad Live: {origin}")

    async def apply_remote(self, changes: list[dict], description: str) -> None:
        """Apply changes to KiCad without letting them bounce back out.

        The board is written FIRST and the baseline updated only on success. The
        old order (baseline first) meant a failed write left the baseline
        claiming values the board never received, so the next poll "detected" a
        local edit undoing the other person's change and sent it to the server.

        If KiCad is busy the changes are queued, in order, and retried; a newer
        change never jumps ahead of an older queued one.
        """
        if self._deferred:
            self._deferred.append((changes, description))
            return
        try:
            self.link.apply_changes(changes, description)
        except KiCadBusy:
            self._deferred.append((changes, description))
            log.warning("KiCad busy - %d change(s) queued, will apply when it responds",
                        len(changes))
            return
        except KiCadUnavailable as exc:
            log.warning("could not apply remote changes: %s", exc)
            return
        self._mark_applied(changes)

    def _mark_applied(self, changes: list[dict]) -> None:
        for change in changes:
            uuid = change.get("uuid")
            if uuid:
                self.echo_marks[uuid] = self._tick
                apply_change(self.baseline, change)

    async def _flush_deferred(self) -> None:
        """Retry queued changes oldest-first. KiCadBusy leaves them queued."""
        while self._deferred:
            changes, description = self._deferred[0]
            self.link.apply_changes(changes, description)
            self._deferred.pop(0)
            self._mark_applied(changes)
            log.info("applied %d queued remote change(s)", len(changes))

    async def _on_whats_new(self, message: dict) -> None:
        """Tell the user what others did while they were away.

        PCB changes are applied automatically by the state sync. Schematic
        changes CANNOT be applied to a running eeschema (it has no IPC API), so
        they are listed here instead of silently missing.
        """
        summary = message.get("summary") or {}
        if summary.get("is_first_visit") or not summary.get("total"):
            return
        counts = summary.get("counts") or {}
        lines = ["Since you were last here: %d schematic change(s), %d PCB change(s), "
                 "%d comment event(s)." % (counts.get("schematic", 0), counts.get("pcb", 0),
                                           counts.get("comments", 0))]
        for event in (summary.get("schematic") or [])[:8]:
            lines.append("   schematic: %s %s" % (event.get("username"), event.get("description")))
        if counts.get("schematic"):
            lines.append("   Schematic edits are NOT applied to your editor: get the updated "
                         ".kicad_sch from the author and reload it.")
        if counts.get("pcb"):
            lines.append("   PCB changes are applied to your board automatically.")
        self.banner("\n  ".join(lines))
        await self.ws.send({"type": "mark_read", "client_id": self.ws.client_id})

    async def _on_change_ack(self, message: dict) -> None:
        for entry in message.get("accepted", []):
            if entry.get("field"):
                self.field_versions[(entry["uuid"], entry["field"])] = int(entry.get("version", 0))

        rejected = message.get("rejected") or []
        if not rejected:
            return

        for entry in rejected:
            if entry.get("reason") == "locked":
                self.stats["blocked"] += 1
                self.banner(f"{entry.get('reference') or entry['uuid'][:8]} is locked by "
                            f"{entry.get('owner_name')} - your change was reverted.")

        # Our optimistic baseline now disagrees with the server for these
        # objects. Rather than guess the correct values, ask for the
        # authoritative state; _on_project_state puts the board back in line.
        await self.request_state()

    async def _on_conflict(self, message: dict) -> None:
        conflicts = message.get("conflicts") or []
        self.stats["conflicts"] += len(conflicts)
        revert: list[dict] = []

        for conflict in conflicts:
            reference = conflict.get("reference") or conflict["uuid"][:8]
            print("\n" + "=" * 62, flush=True)
            print(f"  CONFLICT on {reference}.{conflict.get('field')}", flush=True)
            print(f"    you changed it to : {conflict.get('your_value')}", flush=True)
            print(f"    {conflict.get('conflicting_user')} changed it to : "
                  f"{conflict.get('server_value')}", flush=True)
            print(f"    your base v{conflict.get('your_base_version')}, "
                  f"server is at v{conflict.get('current_version')}", flush=True)
            print("  KiCad Live will not guess. Reverting to the server's value.", flush=True)
            print(f"  To keep YOUR value instead, move {reference} again now.", flush=True)
            print("=" * 62 + "\n", flush=True)

            self.field_versions[(conflict["uuid"], conflict["field"])] = int(
                conflict.get("current_version", 0))
            revert.append({
                "operation": "modify", "object_type": "footprint", "uuid": conflict["uuid"],
                "reference": conflict.get("reference", ""), "field": conflict["field"],
                "new": conflict.get("server_value"),
            })

        if revert:
            await self.apply_remote(revert, "KiCad Live: conflict - reverted to server value")

    async def _on_lock_update(self, message: dict) -> None:
        self.locks = {lock["uuid"]: lock for lock in message.get("locks", [])}
        self.my_locks = {uuid for uuid, lock in self.locks.items()
                         if lock.get("owner") == self.ws.client_id}

    async def _on_lock_granted(self, message: dict) -> None:
        self.my_locks.add(message["uuid"])
        log.info("LOCK   granted on %s", message.get("reference") or message["uuid"][:8])

    async def _on_lock_denied(self, message: dict) -> None:
        reference = message.get("reference") or message["uuid"][:8]
        self.banner(f"{reference} is currently locked by {message.get('owner_name')}. "
                    f"Your edits to it will not be shared until they release it.")

    async def _on_error(self, message: dict) -> None:
        log.warning("server error [%s]: %s", message.get("code"), message.get("message"))

    async def _on_heartbeat(self, message: dict) -> None:
        pass

    async def _on_history(self, message: dict) -> None:
        pass

    async def _on_presence_update(self, message: dict) -> None:
        pass

    async def _on_conflict_event(self, message: dict) -> None:
        pass
