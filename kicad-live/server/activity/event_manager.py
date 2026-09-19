"""The unified activity/event system.

Every meaningful action in KiCad Live becomes one normalised event, whatever
domain it came from. The dashboard, the "what changed since I left" summary
and the change inspector all read from here, so there is exactly one story of
what happened to a project.

Events are recorded first and broadcast second: a dropped WebSocket frame must
never lose history.
"""
from __future__ import annotations

import logging

from server.database.db import Database

log = logging.getLogger("kicadlive.activity")

# Domains
DOMAIN_SCHEMATIC = "schematic"
DOMAIN_PCB = "pcb"
DOMAIN_PROJECT = "project"
DOMAIN_COLLAB = "collab"

DOMAINS = (DOMAIN_SCHEMATIC, DOMAIN_PCB, DOMAIN_PROJECT, DOMAIN_COLLAB)

# Actions, kept to a small closed set so the dashboard can filter on them.
ACTIONS = (
    "connected", "disconnected",
    "added", "modified", "removed",
    "lock_acquired", "lock_released", "lock_denied",
    "conflict_detected", "conflict_resolved",
    "comment_created", "comment_replied", "comment_resolved", "comment_reopened",
    "project_opened", "version_created",
)


class EventManager:
    def __init__(self, database: Database):
        self.db = database
        self._listeners: list = []

    def subscribe(self, callback) -> None:
        """Register an async callback invoked with each new event."""
        self._listeners.append(callback)

    def record(self, project_id: str, domain: str, action: str, description: str,
               user_id: str | None = None, username: str | None = None,
               object_type: str | None = None, object_id: str | None = None,
               object_ref: str | None = None, field: str | None = None,
               old_value=None, new_value=None, version: int | None = None) -> dict:
        """Persist one event and return it. Never raises into the caller."""
        if domain not in DOMAINS:
            domain = DOMAIN_PROJECT
        try:
            event = self.db.add_event(
                project_id=project_id, domain=domain, action=action,
                description=description, user_id=user_id, username=username,
                object_type=object_type, object_id=object_id, object_ref=object_ref,
                field=field, old_value=old_value, new_value=new_value, version=version)
        except Exception:
            # Activity history is valuable but must never break synchronisation.
            log.exception("could not record event (%s/%s)", domain, action)
            return {}
        log.info("[%s] %-10s %-9s %s", project_id, username or "-", domain, description)
        return event

    def record_change(self, project_id: str, domain: str, user_id: str | None,
                      username: str | None, change: dict, summary: str,
                      version: int | None = None) -> dict:
        """Record a structured schematic/PCB change as an event."""
        operation = change.get("operation", "modified")
        action = {"add": "added", "remove": "removed", "modify": "modified"}.get(
            operation, "modified")
        return self.record(
            project_id=project_id, domain=domain, action=action, description=summary,
            user_id=user_id, username=username,
            object_type=change.get("object_type"), object_id=change.get("uuid"),
            object_ref=change.get("reference"), field=change.get("field"),
            old_value=change.get("old"), new_value=change.get("new"), version=version)

    # ----------------------------------------------------------- retrieval

    def recent(self, project_id: str, limit: int = 100, **filters) -> list[dict]:
        return self.db.events(project_id, limit=limit, **filters)

    def detail(self, project_id: str, event_id: int) -> dict | None:
        return self.db.event_by_id(project_id, event_id)

    # --------------------------------------- "what changed since I left?"

    def since_last_read(self, project_id: str, user_id: str, limit: int = 200) -> dict:
        """Everything that happened since this user last marked the feed read.

        Their own actions are excluded - "what changed" means what OTHER people
        did while you were away.
        """
        last_read = self.db.last_read_event(project_id, user_id)
        events = self.db.events(project_id, limit=limit, since_event_id=last_read)
        others = [e for e in events if e.get("user_id") != user_id]

        buckets: dict[str, list[dict]] = {
            DOMAIN_SCHEMATIC: [], DOMAIN_PCB: [], "comments": [], "other": []}
        for event in others:
            if event["action"].startswith("comment_"):
                buckets["comments"].append(event)
            elif event["domain"] in (DOMAIN_SCHEMATIC, DOMAIN_PCB):
                buckets[event["domain"]].append(event)
            else:
                buckets["other"].append(event)

        contributors: dict[str, int] = {}
        for event in others:
            name = event.get("username")
            if name:
                contributors[name] = contributors.get(name, 0) + 1

        previous = self.db.previous_session(project_id, user_id)

        return {
            "user_id": user_id,
            "last_read_event_id": last_read,
            "latest_event_id": self.db.latest_event_id(project_id),
            "total": len(others),
            "is_first_visit": last_read == 0 and previous is None,
            "schematic": buckets[DOMAIN_SCHEMATIC],
            "pcb": buckets[DOMAIN_PCB],
            "comments": buckets["comments"],
            "other": buckets["other"],
            "counts": {
                "schematic": len(buckets[DOMAIN_SCHEMATIC]),
                "pcb": len(buckets[DOMAIN_PCB]),
                "comments": len(buckets["comments"]),
            },
            "contributors": [{"username": n, "changes": c}
                             for n, c in sorted(contributors.items(),
                                                key=lambda kv: -kv[1])],
            "previous_session": previous,
        }

    def mark_read(self, project_id: str, user_id: str, event_id: int | None = None) -> int:
        return self.db.mark_read(project_id, user_id, event_id)

    # ---------------------------------------------------------- statistics

    def stats(self, project_id: str, since_ts: float | None = None) -> dict:
        events = self.db.events(project_id, limit=1000, since_ts=since_ts)
        by_domain: dict[str, int] = {}
        by_user: dict[str, int] = {}
        for event in events:
            by_domain[event["domain"]] = by_domain.get(event["domain"], 0) + 1
            if event.get("username"):
                by_user[event["username"]] = by_user.get(event["username"], 0) + 1
        return {"total": len(events), "by_domain": by_domain, "by_user": by_user}
