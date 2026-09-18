"""REST API for the project-manager dashboard.

The WebSocket carries live deltas; these endpoints serve page loads and
history queries, which are request/response by nature and much easier to
cache, bookmark and debug over HTTP.

Everything here is read-mostly and local: no external service is contacted by
any endpoint, including the hardware summary.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from common.protocol import ValidationError, validate_project_id

log = logging.getLogger("kicadlive.api")


def build_router(ctx) -> APIRouter:
    """Build the API router bound to the server's shared context."""
    router = APIRouter(prefix="/api")

    def _project(project_id: str) -> str:
        try:
            return validate_project_id(project_id)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ----------------------------------------------------------- overview

    @router.get("/overview")
    async def overview(project: str = "demo_board"):
        project_id = _project(project)
        clients = ctx.presence.snapshot(project_id)
        online = [c for c in clients if c["online"]]
        today_start = ctx.day_start()
        return {
            "project_id": project_id,
            "server_version": ctx.server_version,
            "clients_online": len(online),
            "clients": clients,
            "locks": ctx.locks.snapshot(project_id),
            "lock_count": ctx.locks.count(project_id),
            "changes_today": ctx.db.count_events(project_id, 0)
            if today_start is None else ctx.events_today(project_id),
            "open_comments": ctx.db.count_open_comments(project_id),
            "open_conflicts": ctx.conflict_count(project_id),
            "version": ctx.project_version(project_id),
            "recent_activity": ctx.events.recent(project_id, limit=15),
        }

    # ----------------------------------------------------------- activity

    @router.get("/activity")
    async def activity(project: str = "demo_board", limit: int = 100,
                       user: str | None = None, domain: str | None = None,
                       action: str | None = None, since: float | None = None):
        project_id = _project(project)
        return {
            "project_id": project_id,
            "events": ctx.events.recent(
                project_id, limit=min(max(limit, 1), 500),
                user_id=user, domain=domain, action=action, since_ts=since),
            "filters": {"user": user, "domain": domain, "action": action, "since": since},
        }

    @router.get("/activity/{event_id}")
    async def activity_detail(event_id: int, project: str = "demo_board"):
        project_id = _project(project)
        event = ctx.events.detail(project_id, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        related = ctx.events.recent(project_id, limit=10,
                                    object_id=event.get("object_id")) \
            if event.get("object_id") else []
        return {"event": event, "related": related}

    @router.get("/stats")
    async def stats(project: str = "demo_board", since: float | None = None):
        return ctx.events.stats(_project(project), since_ts=since)

    # ----------------------------------------------------------- comments

    @router.get("/comments")
    async def comments(project: str = "demo_board", status: str = "all",
                       domain: str | None = None, object_id: str | None = None,
                       viewer: str | None = None):
        project_id = _project(project)
        return {
            "project_id": project_id,
            "threads": ctx.comments.threads(project_id, status=status, domain=domain,
                                            object_id=object_id, viewer_id=viewer),
            "counts": ctx.comments.counts(project_id),
        }

    @router.get("/comments/objects")
    async def commented_objects(project: str = "demo_board"):
        return {"objects": ctx.comments.annotated_objects(_project(project))}

    # -------------------------------------------------------------- users

    @router.get("/users")
    async def users(project: str = "demo_board"):
        project_id = _project(project)
        known = ctx.db.users(project_id)
        live = {c["client_id"]: c for c in ctx.presence.snapshot(project_id)}
        merged = []
        for user in known:
            presence = live.get(user["user_id"], {})
            merged.append({**user,
                           "online": presence.get("online", False),
                           "activity": presence.get("activity", "offline"),
                           "selection": presence.get("selection", []),
                           "status_text": presence.get("status_text", "Offline"),
                           "domain": presence.get("domain", "")})
        return {"project_id": project_id, "users": merged}

    # ----------------------------------------------------------- versions

    @router.get("/versions")
    async def versions(project: str = "demo_board", limit: int = 100):
        project_id = _project(project)
        return {"project_id": project_id,
                "versions": ctx.db.versions(project_id, limit=min(max(limit, 1), 500))}

    # ----------------------------------------------------- hardware summary

    @router.get("/summary")
    async def summary(project: str = "demo_board", refresh: bool = False):
        """Locally generated hardware summary. Contacts no external service."""
        project_id = _project(project)
        directory = ctx.project_dir_for(project_id)
        result = ctx.analyzer.summarise(directory, force=refresh)
        result["project_id"] = project_id
        return result

    # ------------------------------------------------- what changed since

    @router.get("/whats-new")
    async def whats_new(project: str = "demo_board", user: str = Query(...)):
        project_id = _project(project)
        return ctx.events.since_last_read(project_id, user)

    @router.get("/schematic")
    async def schematic(project: str = "demo_board"):
        """Structured schematic view: sheets, symbols and their comment badges."""
        project_id = _project(project)
        directory = ctx.project_dir_for(project_id)
        result = ctx.analyzer.summarise(directory)
        schematic_part = result.get("schematic") or {}
        return {
            "project_id": project_id,
            "available": bool(schematic_part),
            "reason": None if schematic_part else
                      "; ".join(result.get("warnings", [])) or "No schematic found",
            "sheets": schematic_part.get("sheets", []),
            "components": schematic_part.get("component_list", []),
            "interfaces": schematic_part.get("interfaces", []),
            "power": schematic_part.get("power", {}),
            "locks": ctx.locks.snapshot(project_id, domain="schematic"),
            "comments": ctx.comments.annotated_objects(project_id),
        }

    @router.get("/pcb")
    async def pcb(project: str = "demo_board"):
        project_id = _project(project)
        directory = ctx.project_dir_for(project_id)
        result = ctx.analyzer.summarise(directory)
        return {
            "project_id": project_id,
            "board": result.get("pcb"),
            "objects": ctx.pcb_objects(project_id),
            "locks": ctx.locks.snapshot(project_id, domain="pcb"),
            "comments": ctx.comments.annotated_objects(project_id),
        }

    return router
