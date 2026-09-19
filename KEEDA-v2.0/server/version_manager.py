"""Append-only change history.

Kept in memory for speed and mirrored to a JSON-lines file so history survives a
server restart. Git is deliberately optional: a demo must not fail because a Git
identity is unconfigured on the machine.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque

from common.diff_engine import summarise
from common.protocol import now, validate_project_id

log = logging.getLogger("kicadlive.history")

MAX_IN_MEMORY = 500


class VersionManager:
    def __init__(self, data_dir: str | None = None):
        self._entries: dict[str, deque] = {}
        self._data_dir = data_dir
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

    def _history_path(self, project_id: str) -> str | None:
        """Resolve the history file, refusing anything outside the data dir."""
        if not self._data_dir:
            return None
        validate_project_id(project_id)          # rejects separators and dots
        root = os.path.abspath(self._data_dir)
        path = os.path.abspath(os.path.join(root, f"{project_id}.history.jsonl"))
        # Containment check: belt and braces on top of the id validation.
        if os.path.commonpath([root, path]) != root:
            log.error("refusing history path outside data dir: %s", path)
            return None
        return path

    def _deque(self, project_id: str) -> deque:
        return self._entries.setdefault(project_id, deque(maxlen=MAX_IN_MEMORY))

    def record(self, project_id: str, version: int, user_name: str, change: dict) -> dict:
        entry = {
            "version": version,
            "ts": now(),
            "user_name": user_name,
            "uuid": change.get("uuid"),
            "reference": change.get("reference"),
            "object_type": change.get("object_type", "footprint"),
            "operation": change.get("operation"),
            "field": change.get("field"),
            "old": change.get("old"),
            "new": change.get("new"),
            "summary": summarise(change),
        }
        self._deque(project_id).append(entry)

        path = self._history_path(project_id)
        if path:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
            except OSError as exc:
                # History is a nice-to-have; never let it break synchronisation.
                log.warning("could not append history for %s: %s", project_id, exc)

        log.info("[%s] v%d  %-10s %s", project_id, version, user_name, entry["summary"])
        return entry

    def load(self, project_id: str) -> int:
        """Restore history from disk. Returns the highest version seen."""
        path = self._history_path(project_id)
        if not path or not os.path.exists(path):
            return 0
        highest = 0
        entries = self._deque(project_id)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # tolerate a torn final line
                    entries.append(entry)
                    highest = max(highest, int(entry.get("version", 0)))
        except OSError as exc:
            log.warning("could not read history for %s: %s", project_id, exc)
            return 0
        if highest:
            log.info("[%s] restored %d history entries (up to v%d)",
                     project_id, len(entries), highest)
        return highest

    def recent(self, project_id: str, limit: int = 50) -> list[dict]:
        entries = list(self._deque(project_id))
        return entries[-limit:][::-1]      # newest first

    def all_entries(self, project_id: str) -> list[dict]:
        return list(self._deque(project_id))
