"""Shared protocol constants and validation.

Both the server and the agent import this module, so there is exactly one
definition of what a valid message looks like. See docs/PROTOCOL.md.
"""
from __future__ import annotations

import re
import time
from typing import Any

PROTOCOL_VERSION = "1.0.0"
SERVER_VERSION = "1.0.0"
AGENT_VERSION = "1.0.0"

DEFAULT_PORT = 8000
HEARTBEAT_INTERVAL = 10.0      # seconds between client heartbeats
CLIENT_TIMEOUT = 30.0          # drop a client silent for this long
LOCK_TTL = 120.0               # seconds before an unrefreshed lock expires
POLL_INTERVAL = 0.25           # agent board poll period
MAX_MESSAGES_PER_SECOND = 200

# Fields of a PCB footprint that we synchronise live.
SYNCED_FIELDS = ("position", "rotation", "layer", "value", "reference")

# Domains. "pcb" is the default everywhere so existing clients are unchanged.
DOMAIN_PCB = "pcb"
DOMAIN_SCHEMATIC = "schematic"
DOMAINS = (DOMAIN_PCB, DOMAIN_SCHEMATIC)

# Schematic fields KiCad Live reports. Schematic changes are REPORTED, never
# applied: eeschema does not expose an IPC API in KiCad 10, so there is no safe
# way to write into a running schematic editor. See docs/SCHEMATIC.md.
SCHEMATIC_FIELDS = ("reference", "value", "lib_id", "position", "rotation",
                    "mirror", "unit", "dnp", "footprint", "text",
                    "start", "end", "sheet_name", "sheet_file")

SCHEMATIC_OBJECT_TYPES = ("symbol", "wire", "junction", "local_label",
                          "global_label", "hierarchical_label", "sheet")

ROLES = ("manager", "designer", "viewer")

ACTIVITIES = ("idle", "viewing", "editing", "routing", "offline")
OPERATIONS = ("add", "modify", "remove")

_RE_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RE_UUID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class ValidationError(ValueError):
    """Raised when an inbound message fails validation."""

    def __init__(self, message: str, code: str = "invalid_message"):
        super().__init__(message)
        self.code = code


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# Identifier validation. Every id that reaches state or the filesystem passes
# through one of these.
# --------------------------------------------------------------------------

def validate_client_id(value: Any) -> str:
    if not isinstance(value, str) or not _RE_CLIENT_ID.match(value):
        raise ValidationError(f"invalid client_id: {value!r}", "invalid_id")
    return value


def validate_project_id(value: Any) -> str:
    if not isinstance(value, str) or not _RE_PROJECT_ID.match(value):
        raise ValidationError(f"invalid project_id: {value!r}", "invalid_id")
    # Defence in depth: these can never form a traversal even if concatenated.
    if value in (".", "..") or "/" in value or "\\" in value:
        raise ValidationError(f"invalid project_id: {value!r}", "invalid_id")
    return value


def validate_uuid(value: Any) -> str:
    if not isinstance(value, str) or not _RE_UUID.match(value):
        raise ValidationError(f"invalid uuid: {value!r}", "invalid_id")
    return value


def validate_user_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("user_name must be a string")
    name = value.strip()
    if not name or len(name) > 48:
        raise ValidationError("user_name must be 1-48 characters")
    return name


def validate_activity(value: Any) -> str:
    # Unknown activities are coerced rather than rejected: a display string is
    # never worth dropping a message over.
    return value if value in ACTIVITIES else "idle"


def validate_selection(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("selection must be a list")
    out: list[str] = []
    for item in value[:32]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:32])
    return out


def validate_message(raw: Any) -> dict:
    """Check the envelope. Per-type checks happen in the handlers."""
    if not isinstance(raw, dict):
        raise ValidationError("message must be a JSON object")
    mtype = raw.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ValidationError("message is missing 'type'")
    return raw


def validate_domain(value: Any) -> str:
    """Domain of an object. Absent means PCB, so older agents keep working."""
    if value is None:
        return DOMAIN_PCB
    if value not in DOMAINS:
        raise ValidationError(f"invalid domain: {value!r}")
    return value


def validate_role(value: Any) -> str:
    return value if value in ROLES else "designer"


def validate_change(raw: Any) -> dict:
    """Validate one entry of a `change` message's `changes` list."""
    if not isinstance(raw, dict):
        raise ValidationError("change entry must be an object")

    operation = raw.get("operation")
    if operation not in OPERATIONS:
        raise ValidationError(f"invalid operation: {operation!r}")

    domain = validate_domain(raw.get("domain"))
    allowed_fields = SCHEMATIC_FIELDS if domain == DOMAIN_SCHEMATIC else SYNCED_FIELDS

    out: dict[str, Any] = {
        "operation": operation,
        "domain": domain,
        "object_type": raw.get("object_type",
                               "symbol" if domain == DOMAIN_SCHEMATIC else "footprint"),
        "uuid": validate_uuid(raw.get("uuid")),
        "reference": str(raw.get("reference", ""))[:32],
    }
    if domain == DOMAIN_SCHEMATIC:
        out["sheet"] = str(raw.get("sheet", "/"))[:120]

    if operation == "modify":
        field = raw.get("field")
        if field not in allowed_fields:
            raise ValidationError(f"invalid or unsupported field: {field!r}")
        out["field"] = field
        out["old"] = raw.get("old")
        out["new"] = raw.get("new")
    elif operation == "add":
        state = raw.get("state")
        if not isinstance(state, dict):
            raise ValidationError("'add' requires a 'state' object")
        out["state"] = state

    base_version = raw.get("base_version", 0)
    if not isinstance(base_version, int) or base_version < 0:
        raise ValidationError("base_version must be a non-negative integer")
    out["base_version"] = base_version
    return out


def error_message(code: str, message: str, context: dict | None = None) -> dict:
    return {
        "type": "error",
        "code": code,
        "message": message,
        "context": context or {},
        "ts": now(),
    }
