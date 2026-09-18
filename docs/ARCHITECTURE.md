# KiCad Live — Architecture

## 1. Feasibility assessment (and one significant deviation)

The brief proposed: an Action Plugin inside KiCad, a file watcher, and
S-expression diffing of `.kicad_pcb` files. That design is workable but it is
the *fragile* version of this problem, for four reasons:

1. **Action Plugins only run when clicked.** Keeping a WebSocket alive inside
   KiCad means running a background thread inside KiCad's wxWidgets event loop.
   If that thread misbehaves, it takes KiCad down with the user's board.
2. **File watching only sees saved files.** The user must press Ctrl+S before
   anyone else sees anything, so "live" is really "live-ish, on save".
3. **Writing the file under a running KiCad** triggers the *"file has changed on
   disk"* dialog, and reloading discards the user's undo history.
4. **Text diffing S-expressions is noisy.** KiCad rewrites formatting, ordering
   and unrelated fields on save, so a text diff reports changes nobody made.

KiCad 9 introduced — and KiCad 10.0.6 ships — an **IPC API** that removes all
four problems at once. It was verified working before any of this was designed
(see `docs/ENVIRONMENT.md`). It lets a *separate process* read and modify the
**live, in-memory board** of a running KiCad, with changes landing on KiCad's
normal undo stack.

**Deviation, stated plainly:** KiCad Live is built on the IPC API and
**structured polling of the live board**, not on a file watcher and not on
in-process WebSockets. There is still a KiCad Action Plugin, but its only job is
to be a convenient **launch button** for the out-of-process agent.

What this buys us, measured rather than claimed:

| Concern | File-watch design | IPC design (this one) |
|---|---|---|
| User must save first | Yes | **No** — changes propagate as they happen |
| "File changed on disk" dialog | Constant | **Never** — we never write the file under KiCad |
| Undo history | Destroyed on reload | **Preserved** — each remote change is one undoable commit |
| A crash in our code | Can take KiCad down | **Cannot** — separate process |
| Diff noise | High (formatting churn) | **Zero** — we read typed objects, not text |
| Detection latency | Save-dependent | ~250 ms poll, ~1.1 ms per poll |

The cost of this choice is honest and small: it requires **KiCad 9+** and a
one-time preference toggle. Both are documented in `GETTING_STARTED.md`.

### What is deliberately NOT attempted

* No character-level or keystroke-level collaborative editing.
* No CRDT. Conflicts are detected by per-object version numbers and, when they
  cannot be resolved safely, **handed back to the humans**.
* No automatic merge of two conflicting edits to the same field.
* No live schematic editing. KiCad 10's eeschema exposes no IPC API, so
  schematic collaboration is review-only — see section 12 and Part 2.

---

## 2. System overview

```text
        COMPUTER 1  (Sync Server)                 COMPUTERS 2..5  (Designers)
 +-------------------------------+
 |  FastAPI + WebSockets :8000   |          +-----------------------------+
 |                               |          |  KiCad 10 (unmodified)      |
 |   /            dashboard      |          |   - IPC API enabled         |
 |   /health      liveness       |          |   - demo_board.kicad_pcb    |
 |   /ws          sync socket    |          +--------------+--------------+
 |                               |                         ^
 |  +-------------------------+  |                         | IPC (local socket)
 |  | PresenceManager         |  |                         v
 |  | LockManager             |  |          +-----------------------------+
 |  | DiffEngine (shared)     |  |   WS     |  KiCad Live Agent           |
 |  | ConflictDetector        |<-+----------+  (separate Python process)  |
 |  | VersionManager          |  |   LAN    |   - polls live board        |
 |  | ProjectState            |  |          |   - applies remote changes  |
 |  +-------------------------+  |          +-----------------------------+
 +-------------------------------+
```

The agent is the only component that ever touches KiCad. The server never opens
a board file and never needs KiCad installed.

## 3. Components and responsibilities

### Agent (`agent/`) — one per designer

| Module | Responsibility |
|---|---|
| `kicad_link.py` | The only file that talks to `kipy`. Connect, read footprints, apply changes, read selection. All KiCad-version risk is contained here. |
| `sync_agent.py` | The loop: poll, diff, classify, send or apply. Owns echo suppression. |
| `ws_client.py` | WebSocket transport with automatic reconnect and heartbeat. |
| `main.py` | CLI entry point and configuration. |

### Server (`server/`) — exactly one, on Computer 1

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app, `/health`, `/ws`, dashboard hosting. |
| `websocket_manager.py` | Connection registry, broadcast, clean disconnect. |
| `project_manager.py` | Authoritative per-project object state and versions. |
| `lock_manager.py` | Soft locks, TTL expiry, release-on-disconnect. |
| `presence_manager.py` | Who is online, what they are doing. |
| `diff_engine.py` | Snapshot to structured change list (shared with the agent). |
| `conflict_detector.py` | Version-based optimistic concurrency checks. |
| `version_manager.py` | Append-only change history, mirrored to disk. |

The diff engine is **shared code** used by both sides. Two implementations of
"what changed" that disagree is the classic source of sync bugs; there is one.

## 4. Data flow — a local change

```text
Designer drags R1 in KiCad
        |
        v
  [live board mutates in KiCad's memory]
        |
        | agent polls every 250 ms
        v
  KiCadLink.read_footprints()          ~1.1 ms
        |
        v
  Snapshot  {uuid: {ref,x,y,rot,layer}}
        |
        v
  DiffEngine.diff(baseline, current)
        |
        +-- no change --> loop
        |
        v
  Is this uuid in the echo-suppression set?
        |
        +-- yes --> drop it, clear the mark, loop      (loop breaker)
        |
        v
  Is this object locked by someone else?
        |
        +-- yes --> REVERT locally, warn the user, loop (lock enforcement)
        |
        v
  Send  {type:"change", base_version:N, changes:[...]}
        |
        v
  SERVER: version check
        |
        +-- base_version != N --> {type:"conflict"}  --> agent shows conflict
        |
        v
  Accept: version N+1, append to history
        |
        v
  Broadcast {type:"remote_change"} to every OTHER client
        |
        v
  Each remote agent: mark uuid as echo, apply via commit, update baseline
```

## 5. The loop-breaking rule (file-safety)

An infinite update loop is the classic failure of this kind of system: A's
change arrives at B, B applies it, B's poller sees a change and sends it back to
A, forever. KiCad Live prevents it with a rule that needs no timestamps and no
clock synchronisation:

```text
BEFORE applying a remote change to object U:
    baseline[U] := the value the change says U should become
    echo_marks.add(U)
APPLY the change to KiCad
NEXT POLL:
    current[U] == baseline[U]  ->  diff produces nothing  ->  nothing is sent
```

The baseline is updated to the *expected post-state before the write happens*,
so even if the poll interleaves with the write, the diff is empty. The
`echo_marks` set is a second belt-and-braces guard for the case where KiCad
rounds a coordinate: if a change is detected for a marked uuid on the very next
poll, it is dropped once and the mark is cleared.

Additional file-safety guarantees:

* **The agent never writes the `.kicad_pcb` file.** Only the human's own Ctrl+S
  does. There is therefore no torn-write or corruption path.
* **The server never touches a board file at all.**
* Every remote change is applied inside `begin_commit()` / `push_commit()`, so
  it is atomic and undoable.
* Every commit is wrapped in `try/finally: drop_commit()` so a failure can never
  leave KiCad holding an open commit.
* A client whose `base_version` is stale is refused, not merged.

## 6. Presence

Presence is derived from data KiCad already has, so users do not have to
announce anything:

```text
agent polls board.get_selection()
        |
        v
  selection changed?
        |
        v
  {type:"presence", activity:"editing", selection:["R1"]}
        |
        v
  SERVER PresenceManager  -->  broadcast to all  -->  dashboard
```

Displayed as:

```text
KICAD LIVE
  *  Aditya      Editing R1
  *  Designer B  Viewing U2
  *  Designer C  Idle
  o  Designer D  Offline
```

## 7. Soft locks

Locks are **soft**: they are enforced by the agents, not by KiCad. This is
stated plainly in the UI, because a lock that claims to be stronger than it is
would be worse than no lock at all.

```text
A selects R1  ---> agent auto-requests lock on R1
                        |
                        v
                 LockManager: R1 free?
                        |
          +-------------+-------------+
          | yes                       | no
          v                           v
    lock_granted -> R1 = A      lock_denied(owner=A)
                                      |
                                      v
                        B's agent: refuse to send B's edits to R1,
                        revert R1 locally, show "R1 is locked by A"
```

* Locks auto-release when the selection is cleared.
* Locks expire after a TTL (default 120 s) if an agent goes silent.
* **All of a client's locks are released the moment it disconnects.**

Auto-locking on selection is what makes this usable: the designer never presses
a "lock" button, they just click the part they are about to move.

## 8. Structured diff

Objects are normalised before comparison, so formatting never matters:

```json
{
  "uuid": "33c18730-0af9-4ad4-972d-782bf1b87199",
  "reference": "R1",
  "type": "footprint",
  "position": {"x": 60.0, "y": 50.0},
  "rotation": 0.0,
  "layer": "F.Cu",
  "value": "10k"
}
```

Comparison yields field-level changes:

```json
{
  "operation": "modify",
  "object_type": "footprint",
  "uuid": "33c18730-...",
  "reference": "R1",
  "field": "position",
  "old": {"x": 60.0, "y": 50.0},
  "new": {"x": 65.0, "y": 50.0}
}
```

Positions are converted from nanometres to millimetres and rounded to 1 µm
(3 decimal places) so that floating-point noise cannot manufacture a change.

## 9. Conflict detection

Optimistic concurrency, per object. Every object carries a version; a client
must say which version it is editing from.

```text
   Server: R1 @ v7 (40,30)

   A sends change  base_version=7  (40,30)->(45,30)     [accepted, R1 @ v8]
   B sends change  base_version=7  (40,30)->(40,35)     [B is stale]
                        |
                        v
   CONFLICT
     object   : R1
     field    : position
     base     : (40,30)
     yours    : (40,35)
     theirs   : (45,30)   by A, at v8
     ---> B is asked to choose. Nothing is merged automatically.
```

Two different fields of the same object do **not** conflict; they merge cleanly.
Only same-object/same-field divergence from a common base is a conflict.

When a conflict cannot be resolved safely, the system **stops and asks**. It
never guesses.

## 10. Version history

Every accepted change appends one immutable record:

```text
v14  17:42:31  Aditya  footprint R1  position (40.0,30.0) -> (45.0,30.0)
v15  17:42:44  Rahul   footprint C2  position (80.0,60.0) -> (82.5,60.0)
v16  17:42:58  Priya   footprint U1  rotation 0.0 -> 90.0
```

Kept in memory and mirrored to `history.jsonl` on disk so it survives a server
restart. Git is an *optional* backend for snapshot commits, not a requirement —
the demo must not fail because a Git identity is unconfigured.

## 11. Failure handling

| Failure | Behaviour |
|---|---|
| Server dies | Agents keep KiCad fully usable, retry with backoff, resync on reconnect |
| Agent dies | KiCad is untouched and keeps working; locks expire by TTL |
| KiCad closes | Agent reports `kicad_disconnected`, stays connected to the server |
| Client disconnects | Presence updates; **all its locks release immediately** |
| Client reconnects | Receives full `project_state`; its baseline is replaced wholesale, so it can never push stale data |
| Stale change | Rejected with `conflict`; never silently applied |
| Network blip | Heartbeat every 10 s; a client silent for 30 s is dropped |

**Reconnection is deliberately one-directional:** a returning client *accepts*
server state and discards its own divergent baseline. A client that was offline
cannot overwrite work done while it was away.

## 12. Scope: PCB live, schematic reviewed

PCB collaboration is live because the IPC API exposes board objects richly and
the result is visually obvious.

Schematic collaboration is **detect / broadcast / lock / comment**, not live
editing. This is forced by KiCad, not chosen: eeschema implements no IPC API in
KiCad 10.0.6, so there is no way to write into a running schematic editor, and
writing the file underneath it would be overwritten on the author's next save.

Part 2 (section 15 onwards) covers how the schematic engine, comments, activity
and hardware summary are built on top of this core. Full evidence for the API
finding is in `docs/SCHEMATIC.md`.

## 13. Recommended deployment for the five-computer demo

**Configuration A is recommended.** Computer 1 runs the server *and* may run a
KiCad client; Computers 2–5 run KiCad + agent.

Rationale: the server is an in-memory FastAPI process that idles at a few MB and
near-zero CPU with five clients, so dedicating a whole machine to it wastes a
demo seat. Running a client on Computer 1 also means the presenter can drive the
demo from the machine that shows the dashboard. If Computer 1 turns out to be
the weakest machine, fall back to Configuration B (server only) — nothing in the
code changes, only which processes you start.

## 14. Security posture

This is a **LAN prototype**. Stated plainly:

* No authentication — anyone who can reach the port can connect.
* No encryption — `ws://`, not `wss://`.
* Server binds `0.0.0.0` by default so LAN clients can reach it.

Mitigations that *are* implemented: every inbound message is schema-validated,
project and object IDs are pattern-checked, all filesystem access is confined to
the configured project directory with path-traversal rejection, clients cannot
name arbitrary paths, and no client input is ever passed to a shell.

**Do not run this on an untrusted or public network.**

---

# Part 2 — The collaboration layer

Everything above describes the PCB synchronisation core. This part covers the
project-management layer added on top of it. **None of it changes the PCB
path**: the 99 tests that covered PCB sync before this work still pass
unmodified.

## 15. Two domains, one infrastructure

```text
                      Sync Service
                           |
            +--------------+--------------+
            |                             |
            v                             v
     Schematic Engine                 PCB Engine
   server/schematic/                (project_manager +
   parser.py  diff.py                common/diff_engine)
            |                             |
            v                             v
   file-based, READ ONLY           IPC-based, live read/write
   detected on save                detected by 250 ms poll
```

The two engines are deliberately separate. Schematic objects (symbols, wires,
labels, sheets) and board objects (footprints, tracks, zones) are genuinely
different, and one parser that tried to treat them alike would make both worse.

What **is** shared: connections, presence, locks, the event system, comments,
versioning and the dashboard. A lock is a lock whichever domain it names.

### Why schematic is file-based

KiCad 10's eeschema implements no IPC API. Verified by probe, not assumption:
`GetOpenDocuments`, `GetItems` and `GetSelection` on a schematic document all
return *"no handler available"*, the shipped `schematic_commands_pb2` contains
no commands at all, and there is no symbol type in the schematic protobufs.
`docs/SCHEMATIC.md` has the full evidence.

Writing the `.kicad_sch` under a running eeschema was rejected: eeschema holds
its own in-memory copy and would overwrite the file on the author's next
Ctrl+S, silently destroying work. **KiCad Live opens schematics read-only.**

## 16. Domain-aware locks

Locks are keyed by `(domain, object id)`:

```text
locks["demo_board"]["schematic:33c18730-..."] -> Aditya
locks["demo_board"]["pcb:33c18730-..."]       -> Rahul
```

Both can be held at once, by different people, on the same component. That is
correct: working on a symbol and working on its footprint are different jobs.

`domain` defaults to `"pcb"` everywhere, so an older agent that never sends the
field behaves exactly as before.

## 17. The unified event system

```text
PCB change ----+
Schematic save-+
Lock taken ----+--> EventManager.record() --> SQLite events table
Conflict ------+                                    |
Comment -------+                    +---------------+---------------+
                                    |               |               |
                                    v               v               v
                              Dashboard      "What changed      Change
                              timeline        since I left?"    inspector
```

Every event carries: timestamp, user, **domain**, object type, object id,
object reference, action, field, before, after, description and version.

Events are **recorded first and broadcast second**, so a dropped WebSocket
frame can never lose history. Recording never raises into the caller —
activity is valuable, but it must not be able to break synchronisation.

## 18. Persistence

SQLite, one file, no server process, nothing to install:

| Persisted | Ephemeral by design |
|---|---|
| projects, users, sessions | locks |
| events (the activity timeline) | presence |
| comments and replies | in-memory board state |
| versions | |
| per-user read markers | |

Locks and presence describe who is holding something *right now*. Restoring
them after a restart would resurrect locks owned by nobody, so they are
deliberately not persisted.

Version numbering **continues** across a restart rather than resetting, so a
version number always identifies one specific change.

## 19. Comments

A comment is anchored to an object, never free-floating:

```json
{
  "domain": "schematic", "object_type": "symbol",
  "object_id": "...", "object_ref": "R1",
  "author_name": "Aditya", "text": "Should this be 4.7k?",
  "status": "open", "parent_id": null
}
```

Threads are **one level deep**: a comment on an object, and replies to it. A
reply to a reply joins the original thread. A reply inherits its thread's
target, so a thread can never span two objects.

Comments live only in KiCad Live's database. **Commenting can never modify or
corrupt a design.**

## 20. The hardware summary engine

```text
.kicad_sch --> kicad-cli sch export netlist --format kicadxml
                        |                  (KiCad's own connectivity engine)
                        v
              components / nets / pins / pin functions
                        |
        +---------------+---------------+
        v               v               v
  component_analyzer  net_analyzer   pcb_analyzer  <-- .kicad_pcb parsed directly
        |               |               |
        +---------------+---------------+
                        v
                    summary.py  --> headline + evidence
```

Two rules govern it:

1. **Connectivity is never re-derived.** `kicad-cli` reports what KiCad's own
   engine computed. Reimplementing wire tracing would be slower to write and
   less correct.
2. **Every claim carries its evidence, and no evidence means no claim.** An
   interface is reported only when the schematic names those signals or their
   pin functions. A part being *capable* of USB is not evidence of USB.
   Unknowns read `Not available` with a reason.

**No network access and no AI service.** Proven, not asserted: a test blocks
`socket.socket` entirely and the summary still succeeds, and a second test
fails if any module in `server/project_analysis/` ever imports an HTTP client.

## 21. Dashboard architecture

```text
Browser
  |  REST (page loads, history queries)      GET /api/overview, /activity,
  |------------------------------------>         /comments, /summary, ...
  |
  |  WebSocket (live deltas)                presence_update, lock_update,
  |<------------------------------------    activity, comment_created,
                                            conflict_event, blocked_event
```

One socket, already built for synchronisation, carries the live updates; REST
serves the request/response queries that are easier to cache and debug over
HTTP.

The frontend is **dependency-free vanilla JavaScript**: no framework, no build
step, no CDN. Five demo machines should need nothing installed beyond a
browser, and the page must load with the internet disconnected.

## 22. Roles

`manager`, `designer`, `viewer` — stored per user per project, and used to
label the UI. **They are not security.** There is no authentication; anyone who
can reach the port can connect as anyone. This remains a LAN prototype, and the
docs say so wherever roles are mentioned.
