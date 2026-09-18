"""Measure KiCad Live's behaviour with 2, 3, 4 and 5 clients.

No KiCad needed - these are protocol-level clients, so the numbers isolate the
server's own cost. Board-side latency is measured separately by
tests/manual/test_live_sync.py.

    python tools/benchmark.py                       (against a local server)
    python tools/benchmark.py --server 192.168.1.50 (against the demo server)

Reports connection time, broadcast fan-out latency, and throughput. Every
number printed is observed, not predicted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid as uuidlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

ROUNDS = 25


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


class BenchClient:
    def __init__(self, url, name, project, client_id):
        self.url = url
        self.name = name
        self.project = project
        self.client_id = client_id
        self.ws = None
        self.counter = 0
        # Unique per run: the server treats a repeated change_id as a replay.
        self.run_token = uuidlib.uuid4().hex[:6]
        self.versions = {}

    async def connect(self):
        started = time.perf_counter()
        self.ws = await websockets.connect(self.url, open_timeout=15)
        await self.ws.send(json.dumps({"type": "hello", "client_id": self.client_id,
                                       "user_name": self.name, "project_id": self.project}))
        await self.wait_for("project_state", timeout=15)
        return (time.perf_counter() - started) * 1000

    async def wait_for(self, mtype, timeout=15.0, predicate=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(mtype)
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            self._track(message)
            if message.get("type") == mtype and (predicate is None or predicate(message)):
                return message

    def _track(self, message):
        if message.get("type") == "project_state":
            for uid, obj in (message.get("objects") or {}).items():
                for field, version in (obj.get("field_versions") or {}).items():
                    self.versions[(uid, field)] = int(version)
        elif message.get("type") == "remote_change":
            for change in message.get("changes", []):
                if change.get("field"):
                    self.versions[(change["uuid"], change["field"])] = int(change.get("version", 0))
        elif message.get("type") == "change_ack":
            for entry in message.get("accepted", []):
                if entry.get("field"):
                    self.versions[(entry["uuid"], entry["field"])] = int(entry.get("version", 0))

    async def seed(self, objects):
        changes = [{"operation": "add", "object_type": "footprint", "uuid": uid,
                    "reference": ref, "base_version": 0,
                    "state": {"uuid": uid, "reference": ref, "object_type": "footprint",
                              "position": {"x": x, "y": y}, "rotation": 0.0,
                              "layer": "F.Cu", "value": ref}}
                   for uid, ref, x, y in objects]
        self.counter += 1
        await self.ws.send(json.dumps({"type": "change", "client_id": self.client_id,
                                       "change_id": f"{self.client_id}-{self.run_token}-{self.counter}",
                                       "changes": changes}))
        await self.wait_for("change_ack")

    async def move(self, uid, ref, x, y):
        self.counter += 1
        await self.ws.send(json.dumps({
            "type": "change", "client_id": self.client_id,
            "change_id": f"{self.client_id}-{self.run_token}-{self.counter}",
            "changes": [{"operation": "modify", "object_type": "footprint", "uuid": uid,
                         "reference": ref, "field": "position",
                         "base_version": self.versions.get((uid, "position"), 0),
                         "old": None, "new": {"x": x, "y": y}}]}))

    async def close(self):
        if self.ws:
            await self.ws.close()


async def run_round(url, client_count, project):
    uid = uuidlib.uuid4().hex
    object_uuid = f"{uid[:8]}-{uid[8:12]}-4{uid[13:16]}-8{uid[17:20]}-{uid[20:32]}"

    clients = []
    connect_times = []
    for index in range(client_count):
        client = BenchClient(url, f"Bench {index}", project, f"bench-{project}-{index}")
        connect_times.append(await client.connect())
        clients.append(client)

    await clients[0].seed([(object_uuid, "R1", 50.0, 50.0)])
    for other in clients[1:]:
        await other.wait_for("remote_change")

    sender, receivers = clients[0], clients[1:]
    fanout = []
    ack_times = []

    for round_index in range(ROUNDS):
        x = 50.0 + (round_index + 1) * 0.1
        started = time.perf_counter()
        await sender.move(object_uuid, "R1", x, 50.0)

        await sender.wait_for("change_ack")
        ack_times.append((time.perf_counter() - started) * 1000)

        await asyncio.gather(*[
            receiver.wait_for(
                "remote_change",
                predicate=lambda m, want=x: any(
                    c.get("uuid") == object_uuid and (c.get("new") or {}).get("x") == want
                    for c in m.get("changes", [])))
            for receiver in receivers])
        fanout.append((time.perf_counter() - started) * 1000)

    # Throughput: how fast can one client push changes and have all peers see them?
    burst = 40
    started = time.perf_counter()
    for round_index in range(burst):
        await sender.move(object_uuid, "R1", 60.0 + round_index * 0.01, 50.0)
    for _ in range(burst):
        await sender.wait_for("change_ack")
    elapsed = time.perf_counter() - started

    for client in clients:
        await client.close()

    return {
        "clients": client_count,
        "connect_ms": statistics.mean(connect_times),
        "ack_ms": statistics.mean(ack_times),
        "ack_p95": percentile(ack_times, 0.95),
        "fanout_ms": statistics.mean(fanout),
        "fanout_p95": percentile(fanout, 0.95),
        "fanout_max": max(fanout),
        "throughput": burst / elapsed,
    }


async def main_async(args):
    url = f"ws://{args.server}:{args.port}/ws"
    print("=" * 78)
    print(f" KiCad Live benchmark against {url}")
    print(f" {ROUNDS} round trips per client count, plus a 40-change burst")
    print("=" * 78)
    print()
    print(f"{'Clients':>7}  {'Connect':>9}  {'Ack avg':>9}  {'Ack p95':>9}  "
          f"{'Fanout avg':>11}  {'Fanout p95':>11}  {'Fanout max':>11}  {'Changes/s':>10}")
    print("-" * 78)

    rows = []
    for client_count in args.counts:
        try:
            row = await run_round(url, client_count, f"bench{client_count}_{uuidlib.uuid4().hex[:6]}")
        except Exception as exc:
            print(f"{client_count:>7}  FAILED: {exc}")
            continue
        rows.append(row)
        print(f"{row['clients']:>7}  {row['connect_ms']:>8.1f}ms  {row['ack_ms']:>8.1f}ms  "
              f"{row['ack_p95']:>8.1f}ms  {row['fanout_ms']:>10.1f}ms  "
              f"{row['fanout_p95']:>10.1f}ms  {row['fanout_max']:>10.1f}ms  "
              f"{row['throughput']:>10.1f}")

    print()
    print("Connect  = hello to full project_state")
    print("Ack      = change sent to change_ack received")
    print("Fanout   = change sent until EVERY other client has it")
    print()
    print("Board-side latency (KiCad to KiCad) is measured separately by")
    print("tests/manual/test_live_sync.py and adds roughly one poll interval.")
    return rows


def main():
    parser = argparse.ArgumentParser(description="KiCad Live benchmark")
    parser.add_argument("--server", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--counts", type=int, nargs="+", default=[2, 3, 4, 5])
    args = parser.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
