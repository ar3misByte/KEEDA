"""Authoritative project state and the accept/reject decision for changes.

The server is the single source of truth for object state. It never reads a
board file: state is seeded by the first client that connects, and evolves
purely through accepted changes.
"""
from __future__ import annotations

import logging

from common.diff_engine import apply_change
from common.protocol import now
from server.conflict_detector import detect
from server.version_manager import VersionManager

log = logging.getLogger("kicadlive.project")


class Project:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.version = 0
        self.objects: dict[str, dict] = {}
        self.seeded = False
        self.created_at = now()
        # change_id -> ack payload, so a retried change is never applied twice.
        self.applied_change_ids: dict[str, dict] = {}

    def object_count(self) -> int:
        return len(self.objects)

    def to_state_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "version": self.version,
            "objects": self.objects,
            "seeded": self.seeded,
        }


class ProjectManager:
    def __init__(self, versions: VersionManager):
        self._projects: dict[str, Project] = {}
        self._versions = versions

    def get(self, project_id: str) -> Project:
        project = self._projects.get(project_id)
        if project is None:
            project = Project(project_id)
            # Continue numbering from any history left by a previous run, so
            # versions never go backwards across a server restart.
            restored = self._versions.load(project_id)
            if restored:
                project.version = restored
            self._projects[project_id] = project
            log.info("project created: %s (starting at v%d)", project_id, project.version)
        return project

    def projects(self) -> list[Project]:
        return list(self._projects.values())

    def submit(self, project_id: str, client_id: str, user_name: str,
               change_id: str, changes: list[dict],
               blocked_checker=None) -> dict:
        """Validate and apply a batch of changes.

        `blocked_checker(uuid) -> lock_or_None` lets the caller veto changes to
        objects locked by someone else.

        Returns {"accepted": [...], "rejected": [...], "conflicts": [...],
                 "broadcast": [...], "history": [...], "duplicate": bool}
        """
        project = self.get(project_id)

        # Scope the replay guard by CLIENT as well as change_id: two clients
        # numbering their own changes must never collide with each other.
        dedup_key = f"{client_id}:{change_id}" if change_id else ""

        if dedup_key and dedup_key in project.applied_change_ids:
            # Idempotent retry: acknowledge without re-applying.
            log.info("[%s] duplicate change_id %s ignored", project_id, change_id)
            result = dict(project.applied_change_ids[dedup_key])
            result["duplicate"] = True
            result["broadcast"] = []
            result["history"] = []
            return result

        accepted: list[dict] = []
        rejected: list[dict] = []
        conflicts: list[dict] = []
        broadcast: list[dict] = []
        history: list[dict] = []

        for change in changes:
            uuid = change.get("uuid")
            if not uuid:
                continue

            # 1. Lock check comes first: a locked object is refused outright,
            #    which is a clearer signal to the user than a conflict.
            if blocked_checker is not None:
                lock = blocked_checker(uuid)
                if lock is not None:
                    rejected.append({
                        "uuid": uuid,
                        "reference": change.get("reference", ""),
                        "field": change.get("field"),
                        "reason": "locked",
                        "owner": lock.owner,
                        "owner_name": lock.owner_name,
                    })
                    log.info("[%s] REJECTED %s.%s - locked by %s",
                             project_id, change.get("reference") or uuid[:8],
                             change.get("field"), lock.owner_name)
                    continue

            # 2. Conflict check against the authoritative record.
            record = project.objects.get(uuid)
            conflict = detect(change, record)
            if conflict is not None:
                conflicts.append(conflict.to_dict())
                log.warning("[%s] %s", project_id, conflict.describe())
                continue

            # 3. Accept.
            project.version += 1
            apply_change(project.objects, change)

            entry = project.objects.setdefault(uuid, {"uuid": uuid})
            entry.setdefault("object_type", change.get("object_type", "footprint"))
            if change.get("reference"):
                entry["reference"] = change["reference"]
            entry["version"] = project.version
            entry["last_writer"] = user_name

            field = change.get("field")
            if field:
                entry.setdefault("field_versions", {})[field] = project.version
                entry.setdefault("field_writers", {})[field] = user_name

            accepted.append({
                "uuid": uuid,
                "reference": change.get("reference", ""),
                "field": field,
                "version": project.version,
            })

            broadcast_change = {
                "operation": change.get("operation"),
                "object_type": change.get("object_type", "footprint"),
                "uuid": uuid,
                "reference": change.get("reference", ""),
                "version": project.version,
            }
            if change.get("operation") == "modify":
                broadcast_change["field"] = field
                broadcast_change["new"] = change.get("new")
                broadcast_change["old"] = change.get("old")
            elif change.get("operation") == "add":
                broadcast_change["state"] = change.get("state")
            broadcast.append(broadcast_change)

            # Seeding a fresh project is bulk data, not interesting history.
            if not (change.get("operation") == "add" and not project.seeded):
                history.append(self._versions.record(project_id, project.version, user_name, change))

        if not project.seeded and project.objects:
            project.seeded = True
            log.info("[%s] seeded with %d objects by %s",
                     project_id, len(project.objects), user_name)

        result = {
            "accepted": accepted,
            "rejected": rejected,
            "conflicts": conflicts,
            "broadcast": broadcast,
            "history": history,
            "duplicate": False,
        }
        if dedup_key:
            project.applied_change_ids[dedup_key] = {
                "accepted": accepted, "rejected": rejected, "conflicts": conflicts,
            }
            # Bound the memory used by the replay guard.
            if len(project.applied_change_ids) > 2000:
                for key in list(project.applied_change_ids)[:1000]:
                    del project.applied_change_ids[key]
        return result

    def force_apply(self, project_id: str, user_name: str, uuid: str,
                    field: str, value, reference: str = "") -> dict:
        """Apply a user's explicit 'keep mine' conflict resolution."""
        project = self.get(project_id)
        project.version += 1
        change = {
            "operation": "modify", "object_type": "footprint", "uuid": uuid,
            "reference": reference, "field": field, "new": value,
            "old": (project.objects.get(uuid) or {}).get(field),
        }
        apply_change(project.objects, change)
        entry = project.objects.setdefault(uuid, {"uuid": uuid})
        entry["version"] = project.version
        entry["last_writer"] = user_name
        entry.setdefault("field_versions", {})[field] = project.version
        entry.setdefault("field_writers", {})[field] = user_name
        history = self._versions.record(project_id, project.version, user_name, change)
        log.info("[%s] CONFLICT RESOLVED by %s: %s.%s forced",
                 project_id, user_name, reference or uuid[:8], field)
        return {
            "version": project.version,
            "broadcast": [{
                "operation": "modify", "object_type": "footprint", "uuid": uuid,
                "reference": reference, "field": field, "new": value,
                "version": project.version,
            }],
            "history": [history],
        }
