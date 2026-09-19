# KiCad Live

> **v2.0** - near-live schematic sync (auto-save -> share -> merge -> auto-reload)
> and a FILE-ONLY mode for computers where KiCad's API is busy/timing out.
> Read [docs/LIVE_SCHEMATIC.md](docs/LIVE_SCHEMATIC.md) first.


A collaborative project-management and synchronisation layer for **unmodified
KiCad**. Multiple designers work on the same project at once, with presence,
soft locks, conflict detection, object-attached comments, a persistent activity
timeline and an offline hardware summary — over a plain LAN, with no cloud
services and no AI APIs.

```text
        COMPUTER 1  (Server + Dashboard)          COMPUTERS 2..5  (Designers)
 +----------------------------------+
 |  FastAPI + WebSockets :8000      |      +-------------------------------+
 |                                  |      |  KiCad 10 (unmodified)        |
 |  Dashboard   Overview / Activity |      |   - pcbnew  (IPC API, live)   |
 |              Comments / Summary  |      |   - eeschema (file, on save)  |
 |                                  |      +---------------+---------------+
 |  PresenceManager   LockManager   |                      ^
 |  ConflictDetector  EventManager  |   WS                 | IPC + file watch
 |  CommentManager    ProjectAnalyzer|<--------+            v
 |  SQLite (data/kicadlive.db)      |   LAN   |  KiCad Live Agent          |
 +----------------------------------+         |  (separate process)        |
                                              +----------------------------+
```

## What it does

**PCB — live**
* Move a footprint; others see it in about a quarter of a second. No saving, no
  file transfer, no reload dialogs. Remote changes land on KiCad's **normal undo
  stack**, so Ctrl+Z works on them.

**Schematic — review**
* Changes are **detected on save, broadcast, locked and discussed** — not
  applied. KiCad 10's eeschema exposes no IPC API, so writing into a running
  schematic editor is impossible; see [SCHEMATIC.md](docs/SCHEMATIC.md) for the
  evidence.

**Both domains**
* **Presence** — who is connected and what they have selected.
* **Soft locks** — selecting a component claims it. Schematic and PCB locks are
  independent, so two people can work on a symbol and its footprint at once.
* **Conflict detection** — same-object, same-field collisions are detected,
  attributed and **handed back to the humans** rather than silently merged.
* **Comments** — threaded review discussions attached to real symbols and
  footprints, with resolve/reopen. Stored in SQLite, never in your KiCad files.
* **Activity timeline** — every change, lock, conflict and comment, filterable,
  with a change inspector.
* **"What changed since I left?"** — a per-user delta on reconnect.
* **Hardware summary** — components, interfaces, power rails and board facts,
  generated locally with **no internet and no AI service**.

## What it does not do

* No character-level collaborative editing. This is structured object
  synchronisation, not Google Docs.
* **Schematic changes are never applied** to anyone's editor (see above).
* PCB sync covers **footprints**; tracks, vias and zones are not synchronised.
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
git clone https://github.com/ar3misByte/KiEDA_live.git
cd KiEDA_live
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

> Read [GETTING_STARTED.md](docs/GETTING_STARTED.md) before a real run. It
> covers the firewall rule and the project-copy rule, which are the two things
> people get wrong.

## Documentation

| Document | What it is for |
|---|---|
| [GETTING_STARTED.md](docs/GETTING_STARTED.md) | Step-by-step setup for five computers. **Start here.** |
| [PROJECT_MANAGER_GUIDE.md](docs/PROJECT_MANAGER_GUIDE.md) | Running a session: monitoring, locks, conflicts, comments |
| [SCHEMATIC.md](docs/SCHEMATIC.md) | How schematic collaboration works, and why it is review-only |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works and why it is built this way |
| [PROTOCOL.md](docs/PROTOCOL.md) | Every WebSocket message |
| [ENVIRONMENT.md](docs/ENVIRONMENT.md) | Verified environment, with real command output |
| [FIVE_COMPUTER_TEST.md](docs/FIVE_COMPUTER_TEST.md) | The multi-machine test procedure |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Symptom / cause / check / fix / verify |
| [DEMO.md](docs/DEMO.md) | 3–5 minute demo script, plus a failure-safe backup |

Test plans, each with observed results: [TEST_PLAN.md](docs/TEST_PLAN.md),
[SCHEMATIC_TEST_PLAN.md](docs/SCHEMATIC_TEST_PLAN.md),
[COMMENTS_TEST_PLAN.md](docs/COMMENTS_TEST_PLAN.md),
[DASHBOARD_TEST_PLAN.md](docs/DASHBOARD_TEST_PLAN.md),
[HARDWARE_SUMMARY_TEST_PLAN.md](docs/HARDWARE_SUMMARY_TEST_PLAN.md).

## Repository layout

```text
KiEDA_live/
├── server/                  Sync service (FastAPI + WebSockets)
│   ├── main.py              app, /health, /ws, dashboard hosting
│   ├── api.py               REST API for the dashboard
│   ├── websocket_manager.py connections and broadcast
│   ├── project_manager.py   authoritative PCB state, accept/reject
│   ├── lock_manager.py      soft locks, domain-aware, with TTL
│   ├── presence_manager.py  who is online, doing what
│   ├── conflict_detector.py per-field optimistic concurrency
│   ├── version_manager.py   append-only PCB history
│   ├── schematic/           schematic engine (parser, diff)
│   ├── comments/            object-attached comment threads
│   ├── activity/            unified event system
│   ├── database/            SQLite persistence
│   └── project_analysis/    offline hardware summary
│
├── agent/                   Runs on each designer's computer
│   ├── kicad_link.py        THE ONLY file that talks to live KiCad
│   ├── schematic_link.py    watches .kicad_sch (read-only, never writes)
│   ├── sync_agent.py        poll, diff, send, apply, echo-suppress
│   ├── ws_client.py         transport with reconnect + heartbeat
│   └── main.py              CLI entry point
│
├── common/                  Shared by server and agent
│   ├── protocol.py          constants and validation
│   ├── diff_engine.py       PCB normalisation and structured diff
│   └── sexpr.py             KiCad S-expression reader
│
├── plugin/                  KiCad Action Plugin (launches the agent)
├── overlay/                 Browser dashboard (plain HTML/CSS/JS)
├── sample_project/          demo_board: .kicad_pcb + .kicad_sch
├── tools/                   setup, diagnostics, benchmark, generators
├── tests/unit/              170 tests, no KiCad needed
├── tests/integration/       real server + real WebSockets
├── tests/manual/            real KiCad: file API, plugin, sync, locks, schematic
└── docs/
```

Structural choices worth knowing:

* **`agent/` is separate from `plugin/`.** Sync runs out-of-process; the plugin
  is only a launcher, so a bug in our code cannot take KiCad down with a
  designer's unsaved board.
* **Schematic and PCB engines are separate** (`server/schematic/` vs the PCB
  path). The two domains are genuinely different; one merged parser would make
  both worse.
* **`common/` is shared.** One definition of "what changed" instead of two that
  can disagree — the classic source of sync bugs.

## Testing

```powershell
python -m pytest tests\unit tests\integration -q      # 200 tests, ~7 s, no KiCad
python tools\check_env.py --server SERVER_IP          # per-machine readiness
python tests\manual\test_live_sync.py                 # real KiCad, PCB end to end
python tests\manual\test_live_locks.py                # real KiCad, locking
python tests\manual\test_live_schematic.py            # real schematic detection
python tools\benchmark.py                             # 2-5 client latency
```

Measured on the development machine:

| | Result |
|---|---|
| Remote PCB change appearing in live KiCad | **0.22 s** |
| PCB edit reaching another client | **0.03 s** |
| Schematic save detected and delivered | **0.80 s** |
| Server fan-out, 5 clients (p95) | **1.3 ms** |
| Hardware summary (63 components, 111 nets) | **≈0.4 s**, offline |
| Board poll cost, 6 footprints | **~1–2 ms** |

The test plans mark what has been run and what has not. The five-computer tests
and the dashboard's visual behaviour are marked **NOT RUN** — five machines and
a browser were not available during development.

## How it works, briefly

**PCB.** KiCad 9 introduced an IPC API that lets an external process read and
modify the *live, in-memory* board of a running KiCad. The agent polls it every
250 ms (about 1 ms per poll), normalises footprints and diffs them. The loop
between clients is broken by updating the baseline to the *expected post-state
before* applying a remote change, so the next poll sees nothing to report.

**Schematic.** eeschema implements no IPC API, so the agent watches the
`.kicad_sch` file and re-parses on save (about 17 ms for a 229 KB schematic).
Changes are reported, never applied — writing the file under a running eeschema
would be overwritten on the author's next Ctrl+S and could lose their work.

**Conflicts.** Per-field optimistic concurrency: every field carries a version
and a client says which version it edited from. Different fields of the same
object merge cleanly; the same field from the same base is a conflict, and
conflicts are never resolved automatically.

**Hardware summary.** KiCad's own `kicad-cli` exports the netlist its schematic
engine computed; the board is parsed directly. Claims are made only with
evidence, and anything unevidenced reads *Not available*.

[ARCHITECTURE.md](docs/ARCHITECTURE.md) has the full reasoning.

## Status

Working and verified on one machine with real KiCad: server, agent, plugin,
dashboard, PCB sync, schematic detection, presence, locks, conflicts, comments,
activity, persistence and the offline hardware summary.

Not yet verified across five physical computers, and the dashboard's rendering
has not been observed in a browser. Both are the operator's next step, with
procedures in `docs/FIVE_COMPUTER_TEST.md` and `docs/DASHBOARD_TEST_PLAN.md`.
