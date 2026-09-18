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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from common.protocol import (
    DEFAULT_PORT, HEARTBEAT_INTERVAL, SERVER_VERSION, ValidationError,
    error_message, now, validate_client_id, validate_change, validate_message,
    validate_project_id, validate_user_name, validate_uuid,
)
from server.lock_manager import LockManager
from server.presence_manager import PresenceManager
from server.project_manager import ProjectManager
from server.version_manager import VersionManager
from server.websocket_manager import Connection, WebSocketManager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERLAY_DIR = os.path.join(ROOT, "overlay")
DATA_DIR = os.environ.get("KICADLIVE_DATA_DIR", os.path.join(ROOT, "data"))

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
                await sockets.send(connection, error_message(
                    "invalid_message", "frame was not valid JSON"))
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

    if not connection.is_dashboard:
        presence.register(client_id, user_name, project_id)

    await sockets.send(connection, {
        "type": "welcome",
        "client_id": client_id,
        "project_id": project_id,
        "server_version": SERVER_VERSION,
        "heartbeat_interval": HEARTBEAT_INTERVAL,
        "client_count": sockets.count(project_id),
        "needs_seed": not project.seeded,
    })
    await send_project_state(connection)
    await sockets.send(connection, {"type": "history", "entries": versions.recent(project_id, 30)})
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
    granted, lock = locks.request(
        connection.project_id, uuid, reference, connection.client_id, connection.user_name)

    if granted:
        await sockets.send(connection, {
            "type": "lock_granted", "uuid": uuid, "reference": lock.reference,
            "owner": lock.owner, "expires_in": round(lock.expires_at - now(), 1),
        })
        await broadcast_locks(connection.project_id)
    else:
        await sockets.send(connection, {
            "type": "lock_denied", "uuid": uuid, "reference": lock.reference or reference,
            "owner": lock.owner, "owner_name": lock.owner_name, "reason": "locked_by_other",
        })
        log.info("[%s] LOCK DENIED %s to %s (held by %s)",
                 connection.project_id, reference or uuid[:8],
                 connection.user_name, lock.owner_name)


async def handle_lock_release(connection: Connection, message: dict) -> None:
    raw_uuid = message.get("uuid")
    if raw_uuid is None:
        released = bool(locks.release_all_for_client(connection.client_id))
    else:
        released = locks.release(connection.project_id, validate_uuid(raw_uuid), connection.client_id)
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

    await push_history(project_id, result["history"])


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


async def handle_disconnect(connection: Connection, message: dict) -> None:
    await connection.websocket.close(code=1000)


HANDLERS = {
    "heartbeat": handle_heartbeat,
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
