# Protocol messages

The full, authoritative protocol specification lives in
[`docs/PROTOCOL.md`](../docs/PROTOCOL.md).

The machine-readable definitions — identifier patterns, validation rules,
message-field checks and the shared constants (heartbeat interval, lock TTL,
poll interval) — live in [`common/protocol.py`](../common/protocol.py), which
both the server and the agent import so there is exactly one definition.
