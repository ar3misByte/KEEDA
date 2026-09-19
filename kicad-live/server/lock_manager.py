"""Soft component locks.

Locks are advisory: the server records who holds what, and agents honour it.
Nothing here can stop a determined user from editing a locked part in KiCad,
and the UI says so.
"""
from __future__ import annotations

import logging

from common.protocol import LOCK_TTL, now

log = logging.getLogger("kicadlive.locks")


class Lock:
    __slots__ = ("uuid", "reference", "owner", "owner_name", "acquired_at", "expires_at",
                 "domain", "object_type")

    def __init__(self, uuid: str, reference: str, owner: str, owner_name: str, ttl: float,
                 domain: str = "pcb", object_type: str = "footprint"):
        self.uuid = uuid
        self.reference = reference
        self.domain = domain
        self.object_type = object_type
        self.owner = owner
        self.owner_name = owner_name
        self.acquired_at = now()
        self.expires_at = self.acquired_at + ttl

    def refresh(self, ttl: float) -> None:
        self.expires_at = now() + ttl

    def expired(self) -> bool:
        return now() >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "reference": self.reference,
            "domain": self.domain,
            "object_type": self.object_type,
            "owner": self.owner,
            "owner_name": self.owner_name,
            "acquired_at": self.acquired_at,
            "held_for": round(now() - self.acquired_at, 1),
            "expires_in": max(0.0, round(self.expires_at - now(), 1)),
        }


class LockManager:
    """One lock table per project, covering every domain.

    Locks are keyed by (domain, object id), so a schematic symbol and the PCB
    footprint that realises it are lockable independently - two people can
    legitimately work on the same component in different editors.

    `domain` defaults to "pcb" throughout, so existing PCB callers are
    unchanged.
    """

    def __init__(self, ttl: float = LOCK_TTL):
        self._ttl = ttl
        self._locks: dict[str, dict[str, Lock]] = {}   # project_id -> key -> Lock

    @staticmethod
    def _key(domain: str, uuid: str) -> str:
        return f"{domain}:{uuid}"

    def _table(self, project_id: str) -> dict[str, Lock]:
        return self._locks.setdefault(project_id, {})

    def request(self, project_id: str, uuid: str, reference: str,
                client_id: str, user_name: str, domain: str = "pcb",
                object_type: str = "footprint") -> tuple[bool, Lock]:
        """Try to acquire. Returns (granted, lock_now_in_force)."""
        table = self._table(project_id)
        key = self._key(domain, uuid)
        existing = table.get(key)

        if existing is not None and not existing.expired():
            if existing.owner == client_id:
                # Re-requesting your own lock refreshes it and always succeeds.
                existing.refresh(self._ttl)
                return True, existing
            return False, existing

        if existing is not None and existing.expired():
            log.info("lock on %s expired (was %s), reassigning",
                     reference or uuid, existing.owner_name)

        lock = Lock(uuid, reference, client_id, user_name, self._ttl, domain, object_type)
        table[key] = lock
        log.info("[%s] LOCK GRANTED %s/%s -> %s",
                 project_id, domain, reference or uuid[:8], user_name)
        return True, lock

    def release(self, project_id: str, uuid: str, client_id: str,
                domain: str = "pcb") -> bool:
        """Release one lock. Releasing a lock you do not own is a silent no-op."""
        table = self._table(project_id)
        key = self._key(domain, uuid)
        lock = table.get(key)
        if lock is None or lock.owner != client_id:
            return False
        del table[key]
        log.info("[%s] LOCK RELEASED %s/%s (%s)",
                 project_id, domain, lock.reference or uuid[:8], lock.owner_name)
        return True

    def release_all_for_client(self, client_id: str) -> list[str]:
        """Drop every lock held by a client, in every domain. Called on disconnect."""
        released: list[str] = []
        for project_id, table in self._locks.items():
            for key in [k for k, lk in table.items() if lk.owner == client_id]:
                lock = table[key]
                released.append(lock.uuid)
                log.info("[%s] LOCK RELEASED (disconnect) %s/%s",
                         project_id, lock.domain, lock.reference or lock.uuid[:8])
                del table[key]
        return released

    def expire_stale(self) -> list[str]:
        """Remove timed-out locks. Called by the server's janitor task."""
        expired: list[str] = []
        for project_id, table in self._locks.items():
            for key in [k for k, lk in table.items() if lk.expired()]:
                lock = table[key]
                expired.append(lock.uuid)
                log.info("[%s] LOCK EXPIRED %s/%s (held by %s)",
                         project_id, lock.domain, lock.reference or lock.uuid[:8],
                         lock.owner_name)
                del table[key]
        return expired

    def owner_of(self, project_id: str, uuid: str, domain: str = "pcb") -> Lock | None:
        lock = self._table(project_id).get(self._key(domain, uuid))
        if lock is None or lock.expired():
            return None
        return lock

    def is_blocked_for(self, project_id: str, uuid: str, client_id: str,
                       domain: str = "pcb") -> Lock | None:
        """The lock preventing `client_id` editing `uuid`, or None."""
        lock = self.owner_of(project_id, uuid, domain)
        if lock is not None and lock.owner != client_id:
            return lock
        return None

    def snapshot(self, project_id: str, domain: str | None = None) -> list[dict]:
        locks = [lk for lk in self._table(project_id).values() if not lk.expired()]
        if domain:
            locks = [lk for lk in locks if lk.domain == domain]
        return [lk.to_dict() for lk in locks]

    def count(self, project_id: str) -> int:
        return len([lk for lk in self._table(project_id).values() if not lk.expired()])
