"""WebSocket transport for the agent: reconnect, heartbeat, inbound queue."""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from common.protocol import AGENT_VERSION, HEARTBEAT_INTERVAL

log = logging.getLogger("kicadlive.ws")

RECONNECT_DELAYS = (1, 2, 3, 5, 8, 10)     # seconds, then repeat the last


class WSClient:
    """Maintains a connection to the sync server, reconnecting for ever.

    Inbound messages land on `incoming`. The agent owns all the logic; this
    class only owns the socket.
    """

    def __init__(self, server: str, port: int, client_id: str, user_name: str, project_id: str):
        self.url = f"ws://{server}:{port}/ws"
        self.client_id = client_id
        self.user_name = user_name
        self.project_id = project_id
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.connected = asyncio.Event()
        self._ws = None
        self._attempt = 0
        self._stopping = False
        self.on_connect = None          # optional async callback

    async def send(self, payload: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception as exc:
            log.debug("send failed: %s", exc)
            return False

    async def run(self) -> None:
        """Connect-and-pump loop. Runs until `stop()`."""
        while not self._stopping:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("connection lost: %s", exc)

            if self._stopping:
                break

            delay = RECONNECT_DELAYS[min(self._attempt, len(RECONNECT_DELAYS) - 1)]
            self._attempt += 1
            log.info("reconnecting in %ds (attempt %d)", delay, self._attempt)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        log.info("connecting to %s", self.url)
        async with websockets.connect(self.url, max_size=16 * 1024 * 1024,
                                      open_timeout=10, ping_interval=20) as ws:
            self._ws = ws
            self._attempt = 0
            await self.send({
                "type": "hello", "client_id": self.client_id, "user_name": self.user_name,
                "project_id": self.project_id, "agent_version": AGENT_VERSION,
            })
            self.connected.set()
            log.info("connected to server as '%s'", self.user_name)
            if self.on_connect is not None:
                await self.on_connect()

            heartbeat = asyncio.create_task(self._heartbeat())
            try:
                async for raw in ws:
                    try:
                        await self.incoming.put(json.loads(raw))
                    except json.JSONDecodeError:
                        log.warning("ignoring a non-JSON frame from the server")
            finally:
                heartbeat.cancel()
                self.connected.clear()
                self._ws = None
                log.info("disconnected from server")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not await self.send({"type": "heartbeat", "client_id": self.client_id}):
                return

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
