"""Who is connected and what they are doing."""
from __future__ import annotations

import logging

from common.protocol import CLIENT_TIMEOUT, now, validate_activity, validate_selection

log = logging.getLogger("kicadlive.presence")


class ClientPresence:
    __slots__ = ("client_id", "user_name", "project_id", "activity", "selection",
                 "online", "kicad_connected", "connected_at", "last_seen")

    def __init__(self, client_id: str, user_name: str, project_id: str):
        self.client_id = client_id
        self.user_name = user_name
        self.project_id = project_id
        self.activity = "idle"
        self.selection: list[str] = []
        self.online = True
        self.kicad_connected = False
        self.connected_at = now()
        self.last_seen = now()

    def touch(self) -> None:
        self.last_seen = now()

    def stale(self, timeout: float = CLIENT_TIMEOUT) -> bool:
        return (now() - self.last_seen) > timeout

    def describe(self) -> str:
        """The one-line status the dashboard shows."""
        if not self.online:
            return "Offline"
        if not self.kicad_connected:
            return "No KiCad"
        if self.selection:
            shown = ", ".join(self.selection[:3])
            if len(self.selection) > 3:
                shown += f" +{len(self.selection) - 3}"
            return f"{self.activity.capitalize()} {shown}"
        return self.activity.capitalize()

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "user_name": self.user_name,
            "project_id": self.project_id,
            "activity": self.activity,
            "selection": list(self.selection),
            "online": self.online,
            "kicad_connected": self.kicad_connected,
            "status_text": self.describe(),
            "connected_seconds": round(now() - self.connected_at, 1),
        }


class PresenceManager:
    def __init__(self):
        self._clients: dict[str, ClientPresence] = {}

    def register(self, client_id: str, user_name: str, project_id: str) -> ClientPresence:
        existing = self._clients.get(client_id)
        if existing is not None:
            # A reconnect: keep the record, refresh the identity fields.
            existing.user_name = user_name
            existing.project_id = project_id
            existing.online = True
            existing.touch()
            log.info("client RECONNECTED: %s (%s)", user_name, client_id)
            return existing
        presence = ClientPresence(client_id, user_name, project_id)
        self._clients[client_id] = presence
        log.info("client CONNECTED: %s (%s) project=%s", user_name, client_id, project_id)
        return presence

    def mark_offline(self, client_id: str) -> None:
        presence = self._clients.get(client_id)
        if presence is None:
            return
        presence.online = False
        presence.activity = "offline"
        presence.selection = []
        presence.kicad_connected = False
        log.info("client DISCONNECTED: %s (%s)", presence.user_name, client_id)

    def forget(self, client_id: str) -> None:
        self._clients.pop(client_id, None)

    def update(self, client_id: str, activity=None, selection=None, kicad_connected=None) -> None:
        presence = self._clients.get(client_id)
        if presence is None:
            return
        if activity is not None:
            presence.activity = validate_activity(activity)
        if selection is not None:
            presence.selection = validate_selection(selection)
        if kicad_connected is not None:
            presence.kicad_connected = bool(kicad_connected)
        presence.touch()

    def touch(self, client_id: str) -> None:
        presence = self._clients.get(client_id)
        if presence is not None:
            presence.touch()

    def get(self, client_id: str) -> ClientPresence | None:
        return self._clients.get(client_id)

    def stale_clients(self) -> list[str]:
        return [cid for cid, p in self._clients.items() if p.online and p.stale()]

    def online_count(self, project_id: str | None = None) -> int:
        return sum(1 for p in self._clients.values()
                   if p.online and (project_id is None or p.project_id == project_id))

    def snapshot(self, project_id: str | None = None) -> list[dict]:
        clients = [p for p in self._clients.values()
                   if project_id is None or p.project_id == project_id]
        # Online first, then alphabetical, so the dashboard does not jump around.
        clients.sort(key=lambda p: (not p.online, p.user_name.lower()))
        return [p.to_dict() for p in clients]
