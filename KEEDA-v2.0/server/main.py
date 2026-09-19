"""KiCad Live Sync Server.

Run:
    python -m server.main                 (from the repository root)
    python -m server.main --port 8000 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

if __package__ in (None, ""):
    # Allow `python server/main.py` as well as `python -m server.main`.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from common.protocol import (
    DEFAULT_PORT, HEARTBEAT_INTERVAL, SERVER_VERSION, ValidationError,
    error_message, now, validate_client_id, validate_change, validate_domain,
    validate_message, validate_project_id, validate_role, validate_user_name,
    validate_uuid,
)
from server.activity.event_manager import EventManager
from server.api import build_router
from server.comments.manager import CommentError, CommentManager
from server.database.db import Database
from server.lock_manager import LockManager
from server.project_analysis.summary import ProjectAnalyzer
from server.schematic_files import SchematicFileError, SchematicFileStore
from server.presence_manager import PresenceManager
from server.project_manager import ProjectManager
from server.version_manager import VersionManager
from server.websocket_manager import Connection, WebSocketManager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERLAY_DIR = os.path.join(ROOT, "overlay")
DATA_DIR = os.environ.get("KICADLIVE_DATA_DIR", os.path.join(ROOT, "data"))
# Where the KiCad project lives, for the hardware summary and schematic view.
PROJECT_DIR = os.environ.get("KICADLIVE_PROJECT_DIR",
                             os.path.join(ROOT, "sample_project"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)-22s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kicadlive.server")

STARTED_AT = now()

versions = VersionManager(DATA_DIR)
projects = ProjectManager(versions)
locks = LockManager()
presence = PresenceManager()
sockets = WebSocketManager()

database = Database(os.path.join(DATA_DIR, "kicadlive.db"))
events = EventManager(database)
comments = CommentManager(database, events)
analyzer = ProjectAnalyzer(PROJECT_DIR)
schematic_files = SchematicFileStore(DATA_DIR)

# Conflicts are transient, but the dashboard needs a live count.
open_conflicts: dict[str, list[dict]] = {}
# client_id -> database session id, so sessions can be closed on disconnect.
client_sessions: dict[str, str] = {}


class ServerContext:
    """What the REST API needs from the server, in one place."""

    server_version = SERVER_VERSION
    db = database
    events = events
    comments = comments
    locks = locks
    presence = presence
    analyzer = analyzer

    @staticmethod
    def day_start() -> float:
        import datetime as _dt
        midnight = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()

    @staticmethod
    def events_today(project_id: str) -> int:
        return len(database.events(project_id, limit=1000,
                                   since_ts=ServerContext.day_start()))

    @staticmethod
    def conflict_count(project_id: str) -> int:
        return len(open_conflicts.get(project_id, []))

    @staticmethod
    def project_version(project_id: str) -> int:
        return projects.get(project_id).version

    @staticmethod
    def project_dir_for(project_id: str) -> str:
        return PROJECT_DIR

    @staticmethod
    def pcb_objects(project_id: str) -> list[dict]:
        project = projects.get(project_id)
        return sorted(project.objects.values(),
                      key=lambda o: o.get("reference", ""))


context = ServerContext()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(janitor())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="KiCad Live Sync Server", version=SERVER_VERSION, lifespan=lifespan)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "server_version": SERVER_VERSION,
        "uptime_seconds": round(now() - STARTED_AT, 1),
        "clients": sockets.count(),
        "projects": [
            {"project_id": p.project_id, "version": p.version, "objects": p.object_count()}
            for p in projects.projects()
        ],
    }


@app.get("/api/status")
async def status():
    result = []
    for project in projects.projects():
        result.append({
            "project_id": project.project_id,
            "version": project.version,
            "objects": project.object_count(),
            "clients": presence.snapshot(project.project_id),
            "locks": locks.snapshot(project.project_id),
            "history": versions.recent(project.project_id, 30),
        })
    return {"server_version": SERVER_VERSION, "uptime_seconds": round(now() - STARTED_AT, 1),
            "projects": result}


@app.get("/")
async def dashboard():
    index = os.path.join(OVERLAY_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse({"status": "ok", "message": "dashboard not installed"})


app.include_router(build_router(context))


@app.get("/{page}.html")
async def dashboard_page(page: str):
    """Serve the dashboard's other pages (activity, comments, project...)."""
    if not page.replace("_", "").isalnum():
        return JSONResponse({"error": "not found"}, status_code=404)
    candidate = os.path.join(OVERLAY_DIR, f"{page}.html")
    if os.path.exists(candidate):
        return FileResponse(candidate)
    return JSONResponse({"error": "not found"}, status_code=404)


if os.path.isdir(OVERLAY_DIR):
    app.mount("/static", StaticFiles(directory=OVERLAY_DIR), name="static")


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------

async def broadcast_presence(project_id: str) -> None:
    await sockets.broadcast(project_id, {
        "type": "presence_update",
        "clients": presence.snapshot(project_id),
        "client_count": sockets.count(project_id),
    })


async def broadcast_locks(project_id: str) -> None:
    await sockets.broadcast(project_id, {
        "type": "lock_update",
        "locks": locks.snapshot(project_id),
    })


async def push_history(project_id: str, entries: list[dict]) -> None:
    if entries:
        await sockets.broadcast(project_id, {"type": "history", "entries": entries})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    connection = await sockets.connect(websocket)
    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                # Starlette raises a plain RuntimeError once the peer has gone
                # away, NOT WebSocketDisconnect. Treating that as "malformed
                # frame" and replying puts this loop into a tight spin that
                # starves the event loop and hangs the whole server, so check
                # the socket state before assuming the frame was just bad.
                if websocket.client_state is not WebSocketState.CONNECTED:
                    raise WebSocketDisconnect(code=1006) from None
                if not await sockets.send(connection, error_message(
                        "invalid_message", "frame was not valid JSON")):
                    raise WebSocketDisconnect(code=1006) from None
                continue

            if connection.rate_limited():
                await sockets.send(connection, error_message(
                    "rate_limited", "too many messages per second"))
                continue

            try:
                message = validate_message(raw)
                await handle_message(connection, message)
            except ValidationError as exc:
                await sockets.send(connection, error_message(
                    exc.code, str(exc), {"type": raw.get("type") if isinstance(raw, dict) else None}))
            except WebSocketDisconnect:
                raise
            except Exception:
                log.exception("error handling message from %s", connection.client_id)
                await sockets.send(connection, error_message(
                    "internal_error", "server failed to handle the message"))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket loop failed")
    finally:
        await cleanup_connection(websocket)


async def cleanup_connection(websocket: WebSocket) -> None:
    connection = sockets.disconnect(websocket)
    if connection is None or not connection.registered:
        return
    project_id = connection.project_id or ""
    client_id = connection.client_id or ""

    # A client may have several sockets open only transiently; only tear down
    # presence and locks when this was its last one.
    still_open = any(c.client_id == client_id for c in sockets.connections() if c.registered)
    if still_open:
        return

    presence.mark_offline(client_id)
    released = locks.release_all_for_client(client_id)

    session_id = client_sessions.pop(client_id, None)
    if session_id:
        database.end_session(session_id)
    if not connection.is_dashboard:
        events.record(project_id, "project", "disconnected", "left the project",
                      user_id=client_id, username=connection.user_name)

    await broadcast_presence(project_id)
    if released:
        await broadcast_locks(project_id)
    log.info("[%s] client %s cleaned up (%d locks released, %d remaining)",
             project_id, connection.user_name or client_id, len(released), sockets.count(project_id))


async def handle_message(connection: Connection, message: dict) -> None:
    mtype = message["type"]

    if mtype == "hello":
        await handle_hello(connection, message)
        return

    if not connection.registered:
        await sockets.send(connection, error_message(
            "not_registered", "send 'hello' before any other message"))
        return

    presence.touch(connection.client_id)
    handler = HANDLERS.get(mtype)
    if handler is None:
        await sockets.send(connection, error_message(
            "unknown_type", f"unknown message type: {mtype}", {"type": mtype}))
        return
    await handler(connection, message)


async def handle_hello(connection: Connection, message: dict) -> None:
    if connection.registered:
        await sockets.send(connection, error_message(
            "already_registered", "this connection already sent 'hello'"))
        return
    try:
        client_id = validate_client_id(message.get("client_id"))
        user_name = validate_user_name(message.get("user_name"))
        project_id = validate_project_id(message.get("project_id"))
    except ValidationError as exc:
        log.warning("rejecting bad hello: %s", exc)
        await sockets.send(connection, error_message(exc.code, str(exc)))
        await connection.websocket.close(code=4400)
        return

    connection.client_id = client_id
    connection.project_id = project_id
    connection.user_name = user_name
    connection.is_dashboard = bool(message.get("dashboard"))

    project = projects.get(project_id)

    database.ensure_project(project_id)
    role = validate_role(message.get("role"))
    user = database.ensure_user(project_id, client_id, user_name, role)

    # "What changed since I left?" must be computed BEFORE this session's own
    # events start landing, or the user's own arrival shows up in it.
    whats_new = events.since_last_read(project_id, client_id)

    if not connection.is_dashboard:
        presence.register(client_id, user_name, project_id)
        client_sessions[client_id] = database.start_session(project_id, client_id, user_name)
        events.record(project_id, "project", "connected", "joined the project",
                      user_id=client_id, username=user_name)

    await sockets.send(connection, {
        "type": "welcome",
        "client_id": client_id,
        "project_id": project_id,
        "server_version": SERVER_VERSION,
        "heartbeat_interval": HEARTBEAT_INTERVAL,
        "client_count": sockets.count(project_id),
        "needs_seed": not project.seeded,
        "role": user.get("role", "designer"),
    })
    await send_project_state(connection)
    await sockets.send(connection, {"type": "history", "entries": versions.recent(project_id, 30)})
    await sockets.send(connection, {"type": "whats_new", "summary": whats_new})
    await sockets.send(connection, {
        "type": "schematic_manifest", "files": schematic_files.manifest(project_id)})
    await sockets.send(connection, {
        "type": "comments",
        "threads": comments.threads(project_id, viewer_id=client_id),
        "counts": comments.counts(project_id)})
    await sockets.send(connection, {"type": "activity",
                                    "events": events.recent(project_id, limit=40)})
    await broadcast_presence(project_id)
    await broadcast_locks(project_id)

    log.info("[%s] HELLO from %s (%s)%s -> %d client(s)",
             project_id, user_name, client_id,
             " [dashboard]" if connection.is_dashboard else "", sockets.count(project_id))


async def send_project_state(connection: Connection) -> None:
    project = projects.get(connection.project_id)
    await sockets.send(connection, {
        "type": "project_state",
        "project_id": project.project_id,
        "version": project.version,
        "objects": project.objects,
        "seeded": project.seeded,
        "locks": locks.snapshot(project.project_id),
        "clients": presence.snapshot(project.project_id),
    })


async def handle_heartbeat(connection: Connection, message: dict) -> None:
    await sockets.send(connection, {"type": "heartbeat", "client_id": connection.client_id})


async def handle_presence(connection: Connection, message: dict) -> None:
    presence.update(
        connection.client_id,
        activity=message.get("activity"),
        selection=message.get("selection"),
        kicad_connected=message.get("kicad_connected"),
    )
    await broadcast_presence(connection.project_id)


async def handle_request_state(connection: Connection, message: dict) -> None:
    await send_project_state(connection)


async def handle_request_history(connection: Connection, message: dict) -> None:
    await sockets.send(connection, {
        "type": "history",
        "entries": versions.recent(connection.project_id, int(message.get("limit", 50))),
    })


async def handle_lock_request(connection: Connection, message: dict) -> None:
    uuid = validate_uuid(message.get("uuid"))
    reference = str(message.get("reference", ""))[:32]
    domain = validate_domain(message.get("domain"))
    object_type = str(message.get("object_type")
                      or ("symbol" if domain == "schematic" else "footprint"))[:32]

    granted, lock = locks.request(
        connection.project_id, uuid, reference, connection.client_id,
        connection.user_name, domain=domain, object_type=object_type)

    if granted:
        await sockets.send(connection, {
            "type": "lock_granted", "uuid": uuid, "reference": lock.reference,
            "domain": domain, "owner": lock.owner,
            "expires_in": round(lock.expires_at - now(), 1),
        })
        events.record(connection.project_id, "collab", "lock_acquired",
                      f"locked {domain} {reference or uuid[:8]}",
                      user_id=connection.client_id, username=connection.user_name,
                      object_type=object_type, object_id=uuid, object_ref=reference)
        await broadcast_locks(connection.project_id)
    else:
        await sockets.send(connection, {
            "type": "lock_denied", "uuid": uuid, "reference": lock.reference or reference,
            "domain": domain, "owner": lock.owner, "owner_name": lock.owner_name,
            "reason": "locked_by_other",
        })
        events.record(connection.project_id, "collab", "lock_denied",
                      f"was blocked from {reference or uuid[:8]} "
                      f"(locked by {lock.owner_name})",
                      user_id=connection.client_id, username=connection.user_name,
                      object_type=object_type, object_id=uuid, object_ref=reference)
        log.info("[%s] LOCK DENIED %s/%s to %s (held by %s)",
                 connection.project_id, domain, reference or uuid[:8],
                 connection.user_name, lock.owner_name)


async def handle_lock_release(connection: Connection, message: dict) -> None:
    raw_uuid = message.get("uuid")
    domain = validate_domain(message.get("domain"))
    if raw_uuid is None:
        released = bool(locks.release_all_for_client(connection.client_id))
    else:
        uuid = validate_uuid(raw_uuid)
        released = locks.release(connection.project_id, uuid, connection.client_id,
                                 domain=domain)
        if released:
            events.record(connection.project_id, "collab", "lock_released",
                          f"released {domain} {message.get('reference') or uuid[:8]}",
                          user_id=connection.client_id, username=connection.user_name,
                          object_id=uuid, object_ref=message.get("reference"))
    if released:
        await broadcast_locks(connection.project_id)


async def handle_change(connection: Connection, message: dict) -> None:
    raw_changes = message.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise ValidationError("'changes' must be a non-empty list")
    if len(raw_changes) > 500:
        raise ValidationError("too many changes in one message (max 500)")

    changes = [validate_change(entry) for entry in raw_changes]
    change_id = str(message.get("change_id", ""))[:64]
    project_id = connection.project_id

    # Schematic changes are REPORTED, not synchronised: there is no way to
    # apply them to a running eeschema, so they bypass the PCB state machine
    # entirely and become activity + broadcast only.
    schematic_changes = [c for c in changes if c.get("domain") == "schematic"]
    changes = [c for c in changes if c.get("domain") != "schematic"]

    if schematic_changes:
        await handle_schematic_changes(connection, schematic_changes)
        if not changes:
            await sockets.send(connection, {
                "type": "change_ack", "change_id": change_id,
                "accepted": [{"uuid": c["uuid"], "field": c.get("field"), "version": 0}
                             for c in schematic_changes],
                "rejected": [], "duplicate": False,
            })
            return

    result = projects.submit(
        project_id, connection.client_id, connection.user_name, change_id, changes,
        blocked_checker=lambda uuid: locks.is_blocked_for(project_id, uuid, connection.client_id),
    )

    await sockets.send(connection, {
        "type": "change_ack",
        "change_id": change_id,
        "accepted": result["accepted"],
        "rejected": result["rejected"],
        "duplicate": result["duplicate"],
    })

    if result["conflicts"]:
        await sockets.send(connection, {
            "type": "conflict", "change_id": change_id, "conflicts": result["conflicts"],
        })
        for conflict in result["conflicts"]:
            open_conflicts.setdefault(project_id, []).append(conflict)
            events.record(project_id, "collab", "conflict_detected",
                          f"conflicted with {conflict['conflicting_user']} over "
                          f"{conflict['reference'] or conflict['uuid'][:8]}.{conflict['field']}",
                          user_id=connection.client_id, username=connection.user_name,
                          object_id=conflict["uuid"], object_ref=conflict["reference"],
                          field=conflict["field"],
                          old_value=conflict.get("server_value"),
                          new_value=conflict.get("your_value"))

        # Also tell everyone a conflict happened, so the dashboard can show it.
        # This carries no board data, only who collided over what.
        await sockets.broadcast(project_id, {
            "type": "conflict_event",
            "user_name": connection.user_name,
            "conflicts": [{"reference": c["reference"], "field": c["field"],
                           "conflicting_user": c["conflicting_user"]}
                          for c in result["conflicts"]],
        })

    if result["rejected"]:
        # Surface lock refusals on the dashboard too - this is Test D of the
        # five-computer procedure, and it needs to be visible to an audience.
        await sockets.broadcast(project_id, {
            "type": "blocked_event",
            "user_name": connection.user_name,
            "blocked": [{"reference": r.get("reference"), "owner_name": r.get("owner_name")}
                        for r in result["rejected"] if r.get("reason") == "locked"],
        })

    if result["broadcast"]:
        count = await sockets.broadcast(project_id, {
            "type": "remote_change",
            "origin_client_id": connection.client_id,
            "origin_user_name": connection.user_name,
            "changes": result["broadcast"],
        }, exclude_client=connection.client_id)
        log.info("[%s] BROADCAST %d change(s) from %s to %d client(s)",
                 project_id, len(result["broadcast"]), connection.user_name, count)

    # Seeding a fresh project is bulk data, not interesting activity.
    for entry in result["history"]:
        events.record(project_id, "pcb", "modified", entry.get("summary", "changed the board"),
                      user_id=connection.client_id, username=connection.user_name,
                      object_type=entry.get("object_type"), object_id=entry.get("uuid"),
                      object_ref=entry.get("reference"), field=entry.get("field"),
                      old_value=entry.get("old"), new_value=entry.get("new"),
                      version=entry.get("version"))
        database.add_version(project_id, "pcb", int(entry.get("version") or 0),
                             connection.client_id, connection.user_name,
                             entry.get("summary", ""))

    await push_history(project_id, result["history"])


async def handle_schematic_changes(connection: Connection, changes: list[dict]) -> None:
    """Record and broadcast schematic edits.

    These are review events, not synchronisation: KiCad 10's eeschema has no
    IPC API, so nobody's schematic is modified. Everyone is told what changed
    and by whom, and the object can be locked and commented on.
    """
    from server.schematic.diff import summarise as summarise_schematic

    project_id = connection.project_id
    project = projects.get(project_id)
    recorded = []

    for change in changes:
        # A locked schematic object belongs to someone else; report the clash
        # rather than pretending the edit is shared.
        lock = locks.is_blocked_for(project_id, change["uuid"],
                                    connection.client_id, domain="schematic")
        summary = summarise_schematic(change)
        if lock is not None:
            events.record(project_id, "collab", "lock_denied",
                          f"edited {change.get('reference') or change['uuid'][:8]} in the "
                          f"schematic, which is locked by {lock.owner_name}",
                          user_id=connection.client_id, username=connection.user_name,
                          object_type=change.get("object_type"), object_id=change["uuid"],
                          object_ref=change.get("reference"))
            await sockets.send(connection, {
                "type": "lock_denied", "uuid": change["uuid"], "domain": "schematic",
                "reference": change.get("reference", ""), "owner": lock.owner,
                "owner_name": lock.owner_name, "reason": "locked_by_other",
            })
            continue

        project.version += 1
        event = events.record_change(project_id, "schematic", connection.client_id,
                                     connection.user_name, change, summary,
                                     version=project.version)
        database.add_version(project_id, "schematic", project.version,
                             connection.client_id, connection.user_name, summary)
        if event:
            recorded.append(event)

    if not recorded:
        return

    await sockets.broadcast(project_id, {
        "type": "remote_change",
        "origin_client_id": connection.client_id,
        "origin_user_name": connection.user_name,
        "domain": "schematic",
        "changes": [{**c, "domain": "schematic"} for c in changes],
    }, exclude_client=connection.client_id)
    await sockets.broadcast(project_id, {"type": "activity", "events": recorded})
    log.info("[%s] SCHEMATIC %d change(s) from %s",
             project_id, len(recorded), connection.user_name)


async def handle_comment_create(connection: Connection, message: dict) -> None:
    try:
        comment = comments.create(
            project_id=connection.project_id,
            domain=validate_domain(message.get("domain")),
            object_id=str(message.get("object_id", ""))[:64],
            text=str(message.get("text", "")),
            author_id=connection.client_id,
            author_name=connection.user_name,
            object_type=str(message.get("object_type", ""))[:32] or None,
            object_ref=str(message.get("object_ref", ""))[:32] or None,
            sheet=str(message.get("sheet", ""))[:120] or None,
            parent_id=str(message.get("parent_id", ""))[:32] or None,
        )
    except CommentError as exc:
        await sockets.send(connection, error_message("invalid_message", str(exc)))
        return

    await sockets.broadcast(connection.project_id, {
        "type": "comment_created", "comment": comment,
        "counts": comments.counts(connection.project_id),
    })
    await push_activity(connection.project_id, limit=5)


async def handle_comment_status(connection: Connection, message: dict) -> None:
    try:
        comment = comments.set_status(
            connection.project_id, str(message.get("comment_id", ""))[:32],
            str(message.get("status", "resolved")),
            connection.client_id, connection.user_name)
    except CommentError as exc:
        await sockets.send(connection, error_message("invalid_message", str(exc)))
        return

    await sockets.broadcast(connection.project_id, {
        "type": "comment_updated", "comment": comment,
        "counts": comments.counts(connection.project_id),
    })
    await push_activity(connection.project_id, limit=5)


async def handle_request_comments(connection: Connection, message: dict) -> None:
    await sockets.send(connection, {
        "type": "comments",
        "threads": comments.threads(connection.project_id,
                                    status=str(message.get("status", "all")),
                                    viewer_id=connection.client_id),
        "counts": comments.counts(connection.project_id),
    })


async def handle_request_activity(connection: Connection, message: dict) -> None:
    await sockets.send(connection, {
        "type": "activity",
        "events": events.recent(connection.project_id,
                                limit=int(message.get("limit", 50))),
    })


async def handle_request_whats_new(connection: Connection, message: dict) -> None:
    await sockets.send(connection, {
        "type": "whats_new",
        "summary": events.since_last_read(connection.project_id, connection.client_id),
    })


async def handle_mark_read(connection: Connection, message: dict) -> None:
    event_id = events.mark_read(connection.project_id, connection.client_id,
                                message.get("event_id"))
    await sockets.send(connection, {"type": "marked_read", "event_id": event_id})


async def handle_request_summary(connection: Connection, message: dict) -> None:
    """Locally generated hardware summary - no external service is contacted."""
    summary = analyzer.summarise(PROJECT_DIR, force=bool(message.get("refresh")))
    await sockets.send(connection, {"type": "summary", "summary": summary})


async def push_activity(project_id: str, limit: int = 10) -> None:
    await sockets.broadcast(project_id, {
        "type": "activity", "events": events.recent(project_id, limit=limit)})


async def handle_conflict_resolve(connection: Connection, message: dict) -> None:
    resolution = message.get("resolution")
    if resolution not in ("keep_mine", "keep_theirs"):
        raise ValidationError("resolution must be 'keep_mine' or 'keep_theirs'")

    if resolution == "keep_theirs":
        await sockets.send(connection, {
            "type": "change_ack", "change_id": message.get("change_id", ""),
            "accepted": [], "rejected": [], "duplicate": False,
        })
        return

    uuid = validate_uuid(message.get("uuid"))
    field = message.get("field")
    if field not in ("position", "rotation", "layer", "value", "reference"):
        raise ValidationError(f"invalid field: {field!r}")

    result = projects.force_apply(
        connection.project_id, connection.user_name, uuid, field,
        message.get("value"), str(message.get("reference", ""))[:32])

    await sockets.send(connection, {
        "type": "change_ack", "change_id": message.get("change_id", ""),
        "accepted": [{"uuid": uuid, "field": field, "version": result["version"]}],
        "rejected": [], "duplicate": False,
    })
    await sockets.broadcast(connection.project_id, {
        "type": "remote_change",
        "origin_client_id": connection.client_id,
        "origin_user_name": connection.user_name,
        "changes": result["broadcast"],
    }, exclude_client=connection.client_id)
    await push_history(connection.project_id, result["history"])


async def handle_schematic_push(connection: Connection, message: dict) -> None:
    """Store sheets the author saved so teammates' agents can fetch them."""
    accepted, rejected = [], []
    for item in (message.get("files") or [])[:32]:
        name = item.get("name") if isinstance(item, dict) else None
        try:
            entry = schematic_files.put(
                connection.project_id, name, item.get("content_b64"), item.get("sha256"),
                item.get("base_sha256"), connection.user_name)
            accepted.append({"name": name, "sha256": entry["sha256"], "rev": entry["rev"]})
        except SchematicFileError as exc:
            rejected.append({"name": name, "code": exc.code, "reason": str(exc)})
    await sockets.send(connection, {"type": "schematic_push_result",
                                    "accepted": accepted, "rejected": rejected})
    if accepted:
        log.info("[%s] %s shared schematic sheet(s): %s", connection.project_id,
                 connection.user_name, ", ".join(a["name"] for a in accepted))
        await sockets.broadcast(connection.project_id, {
            "type": "schematic_manifest",
            "files": schematic_files.manifest(connection.project_id)},
            exclude_client=connection.client_id)


async def handle_schematic_pull(connection: Connection, message: dict) -> None:
    import base64
    files = []
    for name in (message.get("names") or [])[:32]:
        found = schematic_files.get(connection.project_id, name)
        if found is None:
            continue
        data, entry = found
        files.append({"name": name, "sha256": entry["sha256"], "author": entry.get("author"),
                      "rev": entry.get("rev"),
                      "content_b64": base64.b64encode(data).decode("ascii")})
    await sockets.send(connection, {"type": "schematic_files", "files": files})


async def handle_request_schematic_manifest(connection: Connection, message: dict) -> None:
    await sockets.send(connection, {
        "type": "schematic_manifest", "files": schematic_files.manifest(connection.project_id)})


async def handle_disconnect(connection: Connection, message: dict) -> None:
    await connection.websocket.close(code=1000)


HANDLERS = {
    "heartbeat": handle_heartbeat,
    "comment_create": handle_comment_create,
    "comment_status": handle_comment_status,
    "request_comments": handle_request_comments,
    "request_activity": handle_request_activity,
    "request_whats_new": handle_request_whats_new,
    "mark_read": handle_mark_read,
    "request_summary": handle_request_summary,
    "schematic_push": handle_schematic_push,
    "schematic_pull": handle_schematic_pull,
    "request_schematic_manifest": handle_request_schematic_manifest,
    "presence": handle_presence,
    "request_state": handle_request_state,
    "request_history": handle_request_history,
    "lock_request": handle_lock_request,
    "lock_release": handle_lock_release,
    "change": handle_change,
    "conflict_resolve": handle_conflict_resolve,
    "disconnect": handle_disconnect,
}


# ---------------------------------------------------------------------------
# Janitor: expire stale locks and drop silent clients
# ---------------------------------------------------------------------------

async def janitor() -> None:
    while True:
        await asyncio.sleep(5.0)
        try:
            touched_projects: set[str] = set()

            if locks.expire_stale():
                touched_projects.update(p.project_id for p in projects.projects())

            for client_id in presence.stale_clients():
                client = presence.get(client_id)
                log.warning("client %s timed out (silent > 30s)",
                            client.user_name if client else client_id)
                presence.mark_offline(client_id)
                locks.release_all_for_client(client_id)
                if client:
                    touched_projects.add(client.project_id)

            for project_id in touched_projects:
                await broadcast_locks(project_id)
                await broadcast_presence(project_id)
        except Exception:
            log.exception("janitor iteration failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="KiCad Live Sync Server")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    args = parser.parse_args()

    import uvicorn

    print("=" * 62)
    print(" KiCad Live Sync Server " + SERVER_VERSION)
    print(f" Listening on   {args.host}:{args.port}")
    print(f" WebSocket      ws://{args.host}:{args.port}/ws")
    print(f" Health         http://{args.host}:{args.port}/health")
    print(f" Dashboard      http://{args.host}:{args.port}/")
    print(f" History data   {DATA_DIR}")
    print("=" * 62)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
