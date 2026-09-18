# KiCad Live

Real-time collaboration for **unmodified KiCad**. Multiple designers work on the
same PCB at once, with presence, soft component locks, structured conflict
detection and version history — over a plain LAN, with no cloud services.

```text
        COMPUTER 1  (Sync Server)                 COMPUTERS 2..5  (Designers)
 +-------------------------------+
 |  FastAPI + WebSockets :8000   |          +-----------------------------+
 |   /         dashboard         |          |  KiCad 10 (unmodified)      |
 |   /health   liveness          |          |   - IPC API enabled         |
 |   /ws       sync socket       |          +--------------+--------------+
 |                               |                         ^
 |   PresenceManager             |                         | IPC (local socket)
 |   LockManager                 |                         v
 |   DiffEngine                  |   WS     +-----------------------------+
 |   ConflictDetector            |<---------+  KiCad Live Agent           |
 |   VersionManager              |   LAN    |  (separate Python process)  |
 +-------------------------------+          +-----------------------------+
```

## What it does

* **Live sync** — move a footprint, others see it in about a quarter of a
  second. No saving, no file transfer, no reload dialogs.
* **Presence** — see who is connected and which component they have selected.
* **Soft locks** — selecting a component claims it; other people's edits to it
  are refused and reverted.
* **Conflict detection** — same-object, same-field collisions are detected,
  attributed, and **handed back to the humans** rather than silently merged.
* **Version history** — every accepted change is versioned, attributed,
  timestamped and persisted.
* **Browser dashboard** — designers, locks and activity, dependency-free.

Remote changes land on KiCad's **normal undo stack**, so Ctrl+Z works on them.

## What it does not do

* No character-level or keystroke-level collaborative editing. This is
  structured object synchronisation, not Google Docs.
* Synchronises **footprints** (position, rotation, layer, value, reference).
  Tracks, vias, zones and schematics are stretch goals.
* Locks are **advisory** — honoured by the agent, not enforced inside KiCad.
* **LAN prototype**: no authentication, no encryption. Do not run it on an
  untrusted network.

## Requirements

| | Required | Tested with |
|---|---|---|
| KiCad | **9.0+** (needs the IPC API) | 10.0.6 |
| Python | 3.11+ | 3.14.7 |
| OS | — | Windows 11 |

**KiCad 8 and earlier will not work.**

## Quick start

```powershell
git clone <YOUR_REPO_URL> kicad-live
cd kicad-live
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Server** (Computer 1):

```powershell
python -m server.main
```

Dashboard at `http://SERVER_IP:8000/`, health at `/health`.

**Each designer** — close KiCad, enable its API, reopen the board, then:

```powershell
python tools\enable_kicad_api.py        # with KiCad CLOSED
python tools\check_env.py --server SERVER_IP
python -m agent.main --server SERVER_IP --name "Designer A"
```

Optional KiCad button — **Tools → External Plugins → KiCad Live**:

```powershell
python tools\install_plugin.py          # with KiCad CLOSED
```

> Read `docs/GETTING_STARTED.md` before a real run. It covers the firewall rule
> and the project-copy rule, which are the two things people get wrong.

## Documentation

| Document | What it is for |
|---|---|
| [GETTING_STARTED.md](docs/GETTING_STARTED.md) | Step-by-step setup for five computers. **Start here.** |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works, and why it is built this way |
| [PROTOCOL.md](docs/PROTOCOL.md) | Every WebSocket message |
| [ENVIRONMENT.md](docs/ENVIRONMENT.md) | Verified environment, with real command output |
| [FIVE_COMPUTER_TEST.md](docs/FIVE_COMPUTER_TEST.md) | The multi-machine test procedure |
| [TEST_PLAN.md](docs/TEST_PLAN.md) | Full test matrix with honest status |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Symptom / cause / check / fix / verify |
| [DEMO.md](docs/DEMO.md) | 3–5 minute demo script, plus a failure-safe backup |

## Repository layout

```text
kicad-live/
├── server/            Sync service (FastAPI + WebSockets)
│   ├── main.py                 app, /health, /ws, dashboard hosting
│   ├── websocket_manager.py    connections and broadcast
│   ├── project_manager.py      authoritative state, accept/reject
│   ├── lock_manager.py         soft locks with TTL
│   ├── presence_manager.py     who is online, doing what
│   ├── conflict_detector.py    per-field optimistic concurrency
│   └── version_manager.py      append-only history
│
├── agent/             Runs on each designer's computer
│   ├── kicad_link.py           THE ONLY file that talks to KiCad
│   ├── sync_agent.py           poll, diff, send, apply, echo-suppress
│   ├── ws_client.py            transport with reconnect + heartbeat
│   └── main.py                 CLI entry point
│
├── common/            Shared by both sides
│   ├── protocol.py             constants and validation
│   └── diff_engine.py          normalisation and structured diff
│
├── plugin/            KiCad Action Plugin (launches the agent)
├── overlay/           Browser dashboard (plain HTML/CSS/JS)
├── sample_project/    demo_board.kicad_pcb - R1 R2 C1 C2 U1 J1
├── tools/             enable_kicad_api, install_plugin, check_env,
│                      test_client, benchmark, make_sample_board
├── tests/unit/        99 automated tests, no KiCad needed
├── tests/integration/   real server + real WebSockets
├── tests/manual/      real KiCad: file API, plugin, live sync, live locks
└── docs/
```

Two structural choices differ from a conventional layout, both deliberate:

* **`agent/` is separate from `plugin/`.** The synchronisation logic runs in its
  own process; the KiCad plugin is only a launcher. A crash in our code
  therefore cannot take KiCad down with a designer's unsaved board.
* **`common/` is shared by server and agent.** One definition of "what changed"
  instead of two that can disagree — the classic source of sync bugs.

## Testing

```powershell
python -m pytest tests\unit tests\integration -q     # 99 tests, ~5 s, no KiCad
python tools\check_env.py --server SERVER_IP         # per-machine readiness
python tests\manual\test_live_sync.py                # real KiCad, end to end
python tests\manual\test_live_locks.py               # real KiCad, locking
python tools\benchmark.py                            # 2-5 client latency
```

Measured on the development machine:

| | Result |
|---|---|
| Remote change appearing in live KiCad | **0.22 s** |
| KiCad edit reaching another client | **0.03 s** |
| Server fan-out, 5 clients (p95) | **1.3 ms** |
| Board poll cost, 6 footprints | **~1–2 ms** |

`docs/TEST_PLAN.md` marks what has been run and what has not. The five-computer
tests are marked NOT RUN because five machines were not available during
development — `docs/FIVE_COMPUTER_TEST.md` is the procedure for closing that gap.

## How it works, briefly

KiCad 9 introduced an **IPC API** that lets an external process read and modify
the *live, in-memory* board of a running KiCad. KiCad Live is built on it rather
than on watching `.kicad_pcb` files, which means: no need to save before others
see a change, no "file changed on disk" dialogs, no lost undo history, and no
diff noise from KiCad's formatting.

The agent polls the live board every 250 ms (about 1 ms per poll), normalises
footprints into comparable structures, and diffs them. The loop between clients
is broken by updating the baseline to the *expected post-state before* applying
a remote change, so the next poll sees nothing to report.

Conflicts use per-field optimistic concurrency: every field carries a version,
and a client says which version it edited from. Two people editing different
fields of the same part merge cleanly; the same field from the same base is a
conflict, and conflicts are never resolved automatically.

`docs/ARCHITECTURE.md` has the full reasoning, including why the file-watching
design in the original brief was rejected.

## Status

Working and verified on one machine with real KiCad: server, agent, plugin,
dashboard, sync, presence, locks, conflicts, history. Not yet verified across
five physical computers — that is the operator's next step.
