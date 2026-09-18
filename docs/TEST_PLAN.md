# KiCad Live — Test Plan

This is the **PCB core's** matrix. The extension features have their own plans,
each with observed results:

* [SCHEMATIC_TEST_PLAN.md](SCHEMATIC_TEST_PLAN.md) — schematic collaboration
* [COMMENTS_TEST_PLAN.md](COMMENTS_TEST_PLAN.md) — comments and threads
* [DASHBOARD_TEST_PLAN.md](DASHBOARD_TEST_PLAN.md) — dashboard and REST API
* [HARDWARE_SUMMARY_TEST_PLAN.md](HARDWARE_SUMMARY_TEST_PLAN.md) — offline summary

## How to run everything

```powershell
# Unit + integration (no KiCad, no manual setup) - 200 tests, ~7 seconds
python -m pytest tests\unit tests\integration -q

# KiCad file API (needs KiCad installed, not running)
& "C:\Program Files\KiCad\10.0\bin\python.exe" tests\manual\test_pcb_api.py

# Plugin install (needs KiCad installed)
& "C:\Program Files\KiCad\10.0\bin\python.exe" tests\manual\test_plugin_loads.py

# Live sync, locking and schematic (needs KiCad OPEN + server running)
python tests\manual\test_live_sync.py
python tests\manual\test_live_locks.py
python tests\manual\test_live_schematic.py

# Environment readiness (per machine)
python tools\check_env.py --server SERVER_IP

# Performance
python tools\benchmark.py --server SERVER_IP
```

## Status legend

**PASS** = observed on the development machine (Windows 11, KiCad 10.0.6,
Python 3.14.7) at the time of writing.
**NOT RUN** = requires hardware not available during development — specifically
the five separate computers. These must be executed by the operator using
`docs/FIVE_COMPUTER_TEST.md`.

Nothing below is marked PASS that was not actually run and seen to pass.

---

## Test matrix

### Environment and KiCad API

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T01 | Environment | Python ≥ 3.11 present | 3.14.7 reported | PASS |
| T02 | KiCad | `pcbnew` importable, version ≥ 9 | 10.0.6 | PASS |
| T03 | KiCad | Action Plugin class available | `hasattr(pcbnew,'ActionPlugin')` true | PASS |
| T04 | KiCad file API | Load board, enumerate footprints | 6 footprints found | PASS |
| T05 | KiCad file API | Modify, save, reload, verify | Modification persists | PASS |
| T06 | KiCad file API | UUID stable across save/reload | Same UUID both times | PASS |
| T07 | IPC API | Connect to running KiCad | Version returned | PASS |
| T08 | IPC API | Read footprints from live board | 6 footprints, positions in nm | PASS |
| T09 | IPC API | Write position via commit | Live board updates, undoable | PASS |
| T10 | IPC API | Write rotation + position together | Both applied | PASS |
| T11 | IPC API | Read current selection | Selection returned | PASS |
| T12 | IPC API | Poll cost | ~1–2 ms for 6 footprints | PASS |
| T13 | Env check | `check_env.py` end to end | All 10 checks pass | PASS |

### Diff engine (unit)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T14 | Units | nm↔mm round trip | Exact | PASS |
| T15 | Noise | 1 nm jitter ignored | No change reported | PASS |
| T16 | Rotation | 0 and 360 equal | No change reported | PASS |
| T17 | Diff | Position change detected | One `modify` | PASS |
| T18 | Diff | Multi-field change | One change per field | PASS |
| T19 | Diff | Add / remove detected | Correct operations | PASS |
| T20 | Diff | Identical snapshots | Empty change list | PASS |
| T21 | Diff | Unsynced fields ignored | No change | PASS |
| T22 | Apply | apply_change then diff | Empty (echo-suppression property) | PASS |

### Protocol validation (unit)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T23 | Validation | Valid ids accepted | Returned unchanged | PASS |
| T24 | Validation | Malformed client ids rejected | `ValidationError` | PASS |
| T25 | Security | Path-traversal project ids rejected | `ValidationError` | PASS |
| T26 | Validation | Unsupported change field rejected | `ValidationError` | PASS |
| T27 | Validation | Negative base_version rejected | `ValidationError` | PASS |
| T28 | Validation | Selection capped at 32 entries | Truncated | PASS |
| T29 | Security | History path traversal refused | `ValidationError` | PASS |

### Locks (unit)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T30 | Lock | First request granted | Granted | PASS |
| T31 | Lock | Second client denied | Denied, owner reported | PASS |
| T32 | Lock | Owner can refresh own lock | Granted | PASS |
| T33 | Lock | Release allows another client | Granted after release | PASS |
| T34 | Lock | Release by non-owner is a no-op | Owner unchanged | PASS |
| T35 | Lock | Disconnect releases all locks | Table empty | PASS |
| T36 | Lock | TTL expiry | Reassignable after timeout | PASS |
| T37 | Lock | Projects isolated | Same uuid lockable per project | PASS |

### Presence (unit)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T38 | Presence | Register and count | Correct online count | PASS |
| T39 | Presence | Disconnect reduces count | Decremented | PASS |
| T40 | Presence | Status text from selection | "Editing R1" | PASS |
| T41 | Presence | Unknown activity coerced | Becomes "idle" | PASS |
| T42 | Presence | Reconnect reuses record | No duplicate client | PASS |
| T43 | Presence | Stale client detected | Listed as stale | PASS |

### Conflicts and versioning (unit)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T44 | Conflict | Up-to-date change accepted | No conflict | PASS |
| T45 | Conflict | Unknown object accepted | No conflict | PASS |
| T46 | Conflict | Stale change to moved object | Conflict raised | PASS |
| T47 | Conflict | Idempotent resend | No conflict | PASS |
| T48 | Conflict | Different fields never conflict | No conflict | PASS |
| T49 | Version | Accepted change bumps version | Incremented | PASS |
| T50 | Version | Duplicate change_id idempotent | Version unchanged | PASS |
| T51 | Version | Locked object rejected not conflicted | reason="locked" | PASS |
| T52 | Version | force_apply wins | Server value overwritten | PASS |
| T53 | History | Recorded and persisted to disk | Entry readable after reload | PASS |
| T54 | History | Versions continue after restart | No regression | PASS |

### Agent logic (unit, fake KiCad)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T55 | Loop safety | Remote change not sent back | Nothing sent | PASS |
| T56 | Loop safety | Echo mark expires | Later real edit IS sent | PASS |
| T57 | Sync | Local edit carries base_version | Correct version | PASS |
| T58 | Sync | Idle poll sends nothing | No traffic | PASS |
| T59 | Lock | Edit to locked object reverted | Board restored, nothing sent | PASS |
| T60 | Lock | Revert does not block later edit | Edit sent after unlock | PASS |
| T61 | Lock | Own lock does not block me | Edit sent | PASS |
| T62 | Conflict | Conflict reverts to server value | Board matches server | PASS |
| T63 | Conflict | Revert not rebroadcast | No traffic | PASS |
| T64 | Read-only | Never sends changes | No traffic | PASS |
| T65 | Read-only | Still applies remote changes | Board updated | PASS |
| T66 | Presence | Selecting requests lock + presence | Both sent | PASS |
| T67 | Presence | Deselecting releases lock | Release sent | PASS |
| T68 | Change ids | Unique across agent restarts | Ids differ | PASS |

### Server integration (real WebSockets, real server process)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T69 | Server | `/health` responds | `{"status":"ok"}` | PASS |
| T70 | WebSocket | Two clients connect | Both registered | PASS |
| T71 | Presence | Both visible with status | Names and status correct | PASS |
| T72 | Sync | Change propagates A→B | B receives correct value | PASS |
| T73 | Loop safety | Originator gets no echo | Timeout waiting for echo | PASS |
| T74 | Lock | Granted then denied then re-granted | Correct sequence | PASS |
| T75 | Lock | Locked object's change rejected | reason="locked" | PASS |
| T76 | Conflict | Concurrent edit detected | Conflict with both values | PASS |
| T77 | Conflict | `keep_mine` resolution wins | Broadcast to others | PASS |
| T78 | Idempotency | Duplicate change_id applied once | No second broadcast | PASS |
| T79 | Disconnect | Locks released, presence updated | Both observed | PASS |
| T80 | Reconnect | Full state restored | Values match server | PASS |
| T81 | 5 clients | All receive every change | All converge | PASS |
| T82 | Robustness | Bad messages do not kill the socket | Errors returned, socket alive | PASS |
| T83 | Protocol | Message before `hello` refused | `not_registered` | PASS |
| T84 | History | Accepted changes recorded | Entry with user and summary | PASS |

### Live KiCad (manual, real KiCad + real server)

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T85 | End to end | Remote change moves live KiCad | Applied in **0.22 s** | PASS |
| T86 | Loop safety | No echo of peer's own change | 0 echoes | PASS |
| T87 | End to end | KiCad edit reaches other client | Received in **0.03 s** | PASS |
| T88 | Stability | No spurious traffic when idle | 0 extra changes | PASS |
| T89 | Lock | Locked component's edit reverted in KiCad | Board restored | PASS |
| T90 | Lock | Blocked change never broadcast | Peer sees nothing | PASS |
| T91 | Lock | Accepted after release | Peer receives it | PASS |
| T92 | Plugin | Installs into KiCad's plugin dir | Files present, config valid | PASS |
| T93 | Plugin | KiCad imports and registers it | `last_load.json` = "loaded" | PASS |

### Performance

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T94 | Perf | 2 clients | fan-out p95 0.8 ms | PASS |
| T95 | Perf | 3 clients | fan-out p95 1.3 ms | PASS |
| T96 | Perf | 4 clients | fan-out p95 1.1 ms | PASS |
| T97 | Perf | 5 clients | fan-out p95 1.3 ms, 3882 changes/s | PASS |

### Five separate computers — **operator must run these**

| ID | Feature | Test | Expected result | Status |
|---|---|---|---|---|
| T98 | 5 machines | Test A — four clients connect | `clients: 4` | NOT RUN |
| T99 | 5 machines | Test B — presence for all four | All listed | NOT RUN |
| T100 | 5 machines | Test C — four independent edits | All converge | NOT RUN |
| T101 | 5 machines | Test D — lock denial across machines | Reverted and refused | NOT RUN |
| T102 | 5 machines | Test E — real conflict | Detected and reported | NOT RUN |
| T103 | 5 machines | Test F — disconnect | Presence + locks update | NOT RUN |
| T104 | 5 machines | Test G — reconnect | State restored | NOT RUN |
| T105 | 5 machines | Test H — server restart | Documented recovery | NOT RUN |
| T106 | Network | LAN reachability + firewall | Health reachable from clients | NOT RUN |
| T107 | Clean machine | GETTING_STARTED followed on a fresh PC | Works without help | NOT RUN |

---

## Coverage gaps, stated honestly

* **Five physical machines were not available during development.** Everything
  that can be simulated in one process has been (T81 runs five concurrent
  clients), but genuine multi-machine behaviour — LAN latency, firewalls, Wi-Fi
  drops, clock differences — is untested. `docs/FIVE_COMPUTER_TEST.md` exists
  precisely to close that gap.
* **Only one KiCad instance was available**, so live tests pair a real agent
  against a KiCad-free peer, and simulate the user's drag through a second IPC
  connection. That is indistinguishable to the agent from a real drag, but it is
  not a human with a mouse.
* **Only Windows was tested.** The code has no Windows-specific logic outside
  the installer and plugin launcher, but Linux/macOS are unverified.
* **Tracks, vias, zones and schematics are not synchronised** — out of MVP
  scope, so there are no tests for them.
* **No authentication or encryption**, therefore no security tests beyond input
  validation and path-traversal rejection.

## Regression tests worth knowing about

Three real bugs were found during development and each has a permanent test:

1. **Per-field versioning** — an edit to `rotation` was reported as conflicting
   because `position` had moved (T48).
2. **Short-circuiting `any()`** — a combined move+rotate only ever moved (T10).
3. **Echo marks never expiring** — the first genuine edit after any remote
   change was silently swallowed (T56), and change ids restarting at 1 made a
   restarted agent's changes look like replays (T68).
