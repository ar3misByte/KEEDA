"""WebSocket connection registry and broadcast."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

from common.protocol import MAX_MESSAGES_PER_SECOND, now

log = logging.getLogger("kicadlive.ws")


class Connection:
    __slots__ = ("websocket", "client_id", "project_id", "user_name",
                 "is_dashboard", "_window_start", "_window_count")

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.client_id: str | None = None
        self.project_id: str | None = None
        self.user_name: str | None = None
        self.is_dashboard = False
        self._window_start = now()
        self._window_count = 0

    @property
    def registered(self) -> bool:
        return self.client_id is not None

    def rate_limited(self) -> bool:
        """Simple fixed-window limiter; enough to stop a runaway agent."""
        current = now()
        if current - self._window_start >= 1.0:
            self._window_start = current
            self._window_count = 0
        self._window_count += 1
        return self._window_count > MAX_MESSAGES_PER_SECOND


class WebSocketManager:
    def __init__(self):
        self._connections: dict[WebSocket, Connection] = {}

    async def connect(self, websocket: WebSocket) -> Connection:
        await websocket.accept()
        connection = Connection(websocket)
        self._connections[websocket] = connection
        return connection

    def disconnect(self, websocket: WebSocket) -> Connection | None:
        return self._connections.pop(websocket, None)

    def get(self, websocket: WebSocket) -> Connection | None:
        return self._connections.get(websocket)

    def connections(self) -> list[Connection]:
        return list(self._connections.values())

    def agents(self, project_id: str | None = None) -> list[Connection]:
        return [c for c in self._connections.values()
                if c.registered and not c.is_dashboard
                and (project_id is None or c.project_id == project_id)]

    def count(self, project_id: str | None = None) -> int:
        return len(self.agents(project_id))

    async def send(self, connection: Connection, payload: dict[str, Any]) -> bool:
        payload.setdefault("ts", now())
        try:
            await connection.websocket.send_json(payload)
            return True
        except Exception as exc:
            # A send failure means the peer is gone; the receive loop will clean up.
            log.debug("send to %s failed: %s", connection.client_id, exc)
            return False

    async def broadcast(self, project_id: str, payload: dict[str, Any],
                        exclude_client: str | None = None,
                        include_dashboards: bool = True) -> int:
        """Send to everyone in a project. Returns the number of recipients."""
        payload.setdefault("ts", now())
        sent = 0
        for connection in list(self._connections.values()):
            if not connection.registered:
                continue
            if connection.project_id != project_id:
                continue
            if connection.is_dashboard and not include_dashboards:
                continue
            if exclude_client and connection.client_id == exclude_client:
                continue
            if await self.send(connection, payload):
                sent += 1
        return sent
