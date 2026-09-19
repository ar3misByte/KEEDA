"""Version-based conflict detection (optimistic concurrency).

The rule is deliberately small enough to reason about:

    A change is a conflict when the client edited from a version of the object
    that is no longer current, AND the value the server now holds for that
    field differs from what the client started from.

Two clients editing *different fields* of the same object merge cleanly. Only
same-object / same-field divergence from a common base is a conflict, and such a
conflict is never resolved automatically.
"""
from __future__ import annotations

from typing import Any

from common.diff_engine import values_equal


class Conflict:
    __slots__ = ("uuid", "reference", "field", "your_base_version", "current_version",
                 "your_value", "server_value", "conflicting_user")

    def __init__(self, uuid: str, reference: str, field: str,
                 your_base_version: int, current_version: int,
                 your_value: Any, server_value: Any, conflicting_user: str):
        self.uuid = uuid
        self.reference = reference
        self.field = field
        self.your_base_version = your_base_version
        self.current_version = current_version
        self.your_value = your_value
        self.server_value = server_value
        self.conflicting_user = conflicting_user

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "reference": self.reference,
            "field": self.field,
            "your_base_version": self.your_base_version,
            "current_version": self.current_version,
            "your_value": self.your_value,
            "server_value": self.server_value,
            "conflicting_user": self.conflicting_user,
        }

    def describe(self) -> str:
        return (f"CONFLICT on {self.reference or self.uuid[:8]}.{self.field}: "
                f"client edited from v{self.your_base_version}, "
                f"server is at v{self.current_version} (changed by {self.conflicting_user})")


def detect(change: dict, record: dict | None) -> Conflict | None:
    """Check one structured change against the server's record for that object.

    `record` is the server's object entry, or None if the object is unknown.
    Returns a Conflict, or None when the change may be accepted.
    """
    operation = change.get("operation")

    # Adding an object nobody has seen is never a conflict.
    if record is None:
        return None

    if operation == "add":
        # Re-adding a known object is treated as a no-op reconciliation,
        # not a conflict; the seeding client may simply be resending.
        return None

    if operation == "remove":
        return None

    field = change.get("field")
    if field is None:
        return None

    # Versions are tracked PER FIELD. Falling back to the object's global
    # version here would make an edit to `rotation` look stale merely because
    # someone else moved `position` - the exact false conflict that would make
    # the system feel broken. A field nobody has written yet is at version 0,
    # so its first writer can never conflict.
    field_versions = record.get("field_versions") or {}
    current_version = int(field_versions.get(field, 0))
    base_version = int(change.get("base_version", 0))

    if base_version >= current_version:
        return None  # Client is up to date on this field.

    # The client is behind. That only matters if the value actually moved away
    # from what they started from: an idempotent resend is not a conflict.
    server_value = record.get(field)
    client_old = change.get("old")
    if values_equal(field, server_value, client_old):
        return None
    if values_equal(field, server_value, change.get("new")):
        return None  # Already exactly what the client wants; nothing to fight over.

    last_writers = record.get("field_writers") or {}
    return Conflict(
        uuid=change.get("uuid", ""),
        reference=change.get("reference", ""),
        field=field,
        your_base_version=base_version,
        current_version=current_version,
        your_value=change.get("new"),
        server_value=server_value,
        conflicting_user=last_writers.get(field, record.get("last_writer", "another user")),
    )
