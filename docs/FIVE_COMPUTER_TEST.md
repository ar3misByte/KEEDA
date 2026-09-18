# KiCad Live — Five-Computer Test Procedure

Run this once, in order, before demoing. It takes about 25 minutes and finds
the one machine where a step was missed.

Each test states what to do and exactly what should happen. Record PASS/FAIL in
the results table at the end — do not mark anything PASS you have not seen.

## Roles

```text
                        COMPUTER 1
                       Sync Server
                      FastAPI + WS :8000
                       + Dashboard
                            |
        +---------+---------+---------+---------+
        |         |         |         |
     COMP 2    COMP 3    COMP 4    COMP 5
   Designer A Designer B Designer C Designer D
     KiCad     KiCad     KiCad     KiCad
     + agent   + agent   + agent   + agent
```

## Before you start

On every designer machine, with KiCad open on `demo_board.kicad_pcb`:

```powershell
python tools\check_env.py --server SERVER_IP
```

All checks must pass. Do not proceed otherwise — `docs/TROUBLESHOOTING.md` has
the fixes.

Critically: **R1's UUID must be identical on all four machines.** Cloning the
repository guarantees this. To confirm:

```powershell
python -c "from agent.kicad_link import KiCadLink; l=KiCadLink(); l.connect(); print({v['reference']: v['uuid'] for v in l.read_snapshot().values()}['R1'])"
```

---

## Test A — Connection

**Do**

1. Computer 1: `python -m server.main`
2. Computer 1: open `http://SERVER_IP:8000/`
3. Computers 2–5, each in turn:
   `python -m agent.main --server SERVER_IP --name "Designer X"`

**Expect**

Each agent prints:

```text
 Board      demo_board.kicad_pcb
 Project    demo_board
connected to server as 'Designer A'
```

Server log shows, for each:

```text
[demo_board] HELLO from Designer A (…) -> 1 client(s)
```

Health endpoint:

```powershell
curl http://SERVER_IP:8000/health
```

```json
{"status":"ok", "clients":4, "projects":[{"project_id":"demo_board","objects":6,...}]}
```

**PASS when** `clients` is **4** and the dashboard's *Designers online* tile
reads **4**.

---

## Test B — Presence

**Do**
On each designer machine in turn, click a different component
(A→R1, B→U1, C→C1, D→J1).

**Expect**
The dashboard's Designers panel:

```text
Designer A    Editing R1
Designer B    Editing U1
Designer C    Editing C1
Designer D    Editing J1
```

Then press **Esc** on Computer 2 to deselect.

**Expect** Designer A changes to `Viewing`.

**PASS when** all four appear with the right component, and deselecting updates
within about a second.

---

## Test C — Independent edits

**Do**
Simultaneously — count down out loud and go:

* A moves **R1**
* B moves **U1**
* C moves **C1**
* D moves **J1**

**Expect**
Every machine ends up showing **all four** parts in their new positions. The
dashboard Activity feed lists four entries with increasing version numbers:

```text
17:42:31  Designer A  moved R1 to (65.00, 50.00)   v15
17:42:31  Designer B  moved U1 to (104.00, 55.00)  v16
17:42:32  Designer C  moved C1 to (84.00, 50.00)   v17
17:42:32  Designer D  moved J1 to (128.00, 55.00)  v18
```

**PASS when** all four boards agree, and no conflicts were reported. Different
components never conflict.

---

## Test D — Same-object lock

**Do**

1. On Computer 2, **click and hold the selection on R1** (just select it).
2. Check the dashboard: `R1 → Designer A`.
3. On Computer 3, select R1 and try to move it.

**Expect**
Computer 3's agent console:

```text
  >>> R1 is currently locked by Designer A. Your edits to it will not be
      shared until they release it.

  >>> R1 is currently locked by Designer A - your change was reverted.
```

R1 on Computer 3 **snaps back** to where it was. The dashboard Activity feed
shows:

```text
17:43:02  Designer B  blocked from editing R1 — locked by Designer A
```

4. On Computer 2, press **Esc** to deselect R1.
5. On Computer 3, move R1 again.

**Expect** it now works and propagates to everyone.

**PASS when** the edit is refused and reverted while locked, and accepted after
release.

> This is verified automatically by `tests/manual/test_live_locks.py`.

---

## Test E — Concurrent conflict

Locks normally prevent conflicts, so force one by editing **without selecting
first** — for example by nudging with arrow keys after selecting and quickly
deselecting, or by running the scripted version below.

**Do**
The reliable way to produce a conflict on demand:

```powershell
# On Computer 1, two KiCad-free clients editing the same field from the same base
python tools\test_client.py --server SERVER_IP --name "Conflict A"
```

Or, with real KiCad: have A and B both move R1 within the same ~250 ms poll
window while neither holds the lock.

**Expect**
The losing machine prints:

```text
==============================================================
  CONFLICT on R1.position
    you changed it to : {'x': 40.0, 'y': 35.0}
    Designer A changed it to : {'x': 45.0, 'y': 30.0}
    your base v7, server is at v8
  KiCad Live will not guess. Reverting to the server's value.
==============================================================
```

Server log:

```text
[demo_board] CONFLICT on R1.position: client edited from v7, server is at v8 (changed by Designer A)
```

Dashboard Activity feed shows a red CONFLICT row.

**PASS when** the conflict is detected, named by user and field, and the losing
board is reverted rather than silently merged.

---

## Test F — Disconnect

**Do**

1. On Computer 4 (Designer C), select **C1** so it holds a lock.
2. Confirm the dashboard shows `C1 → Designer C`.
3. Press **Ctrl+C** in Designer C's agent window.

**Expect**

* Designer C goes to **Offline** on the dashboard within a second.
* `C1 → Designer C` **disappears** from the Locks panel immediately.
* Server log:
  ```text
  client DISCONNECTED: Designer C (…)
  [demo_board] LOCK RELEASED (disconnect) C1
  [demo_board] client Designer C cleaned up (1 locks released, 3 remaining)
  ```
* Designers A, B and D keep working normally — move a part to confirm.

**PASS when** presence updates, the lock is released, and the remaining three
still synchronise.

---

## Test G — Reconnect

**Do**

1. While Designer C is still down, have Designer A move **R2** somewhere obvious.
2. Restart Designer C's agent:
   `python -m agent.main --server SERVER_IP --name "Designer C"`

**Expect**

* Designer C reappears on the dashboard.
* Designer C's agent logs `adopting N field(s) from the server`.
* **R2 on Computer 4 jumps to the position Designer A chose**, even though
  Computer 4 was offline when it moved.
* Nothing Designer C had locally overwrites the others. Reconnection is
  one-directional: a returning client accepts server state.

**PASS when** Computer 4's board matches the others and no stale change is
pushed back.

---

## Test H — Server restart

**Do**

1. Press **Ctrl+C** in the server window.
2. Observe all four agents report the connection lost and begin retrying.
3. Start the server again: `python -m server.main`

**Expect** — this is the documented recovery behaviour:

* All four agents reconnect on their own within about 10 seconds (backoff is
  1, 2, 3, 5, 8, 10 s).
* The first agent to arrive **re-seeds** the project from its live board, so
  object state returns immediately.
* Version numbers **continue upward** rather than resetting, because history is
  read back from `data/demo_board.history.jsonl`.
* Locks are **not** restored — everyone starts unlocked. Re-selecting a
  component re-takes its lock.
* Earlier history is still listed on the dashboard.

Then move a part to confirm synchronisation resumed.

**PASS when** every agent reconnects unaided, versions do not go backwards, and
changes flow again.

---

## Test I — All five at once (optional, Configuration A)

If Computer 1 also runs KiCad, start a fifth agent there and repeat Test C with
five simultaneous movers.

**PASS when** `clients` is 5 and all five boards converge.

---

## Performance check

From any machine:

```powershell
python tools\benchmark.py --server SERVER_IP
```

Measured on the development machine (server and clients on one host, so these
isolate server cost and exclude LAN latency):

```text
Clients    Connect    Ack avg    Ack p95   Fanout avg   Fanout p95   Fanout max   Changes/s
      2      19.0ms      2.9ms      0.7ms       3.0ms        0.8ms       60.8ms      5226.2
      3       2.2ms      0.6ms      0.9ms       0.7ms        1.3ms        1.3ms      4292.9
      4       2.1ms      0.6ms      0.7ms       0.8ms        1.1ms        1.3ms      4017.5
      5       2.4ms      0.6ms      1.0ms       0.9ms        1.3ms        1.4ms      3882.4
```

Read this as: the server is not the bottleneck. Fan-out to every client stays
**under 1.5 ms at the 95th percentile with five clients**. End-to-end latency is
dominated by the agent's **250 ms** board poll, plus LAN round-trip.

Board-side latency, measured by `tests/manual/test_live_sync.py`:

| Path | Measured |
|---|---|
| Remote change appearing in live KiCad | 0.22 s |
| KiCad edit reaching another client | 0.03 s |

Expect **well under one second** end to end on a normal LAN. Re-run the
benchmark from a *client* machine to include real network time.

---

## Results table

Fill this in as you go. Do not mark PASS for anything you have not observed.

| Test | What it proves | Result | Notes |
|---|---|---|---|
| A — Connection | 4 clients connect and are counted | | |
| B — Presence | Everyone visible with their selection | | |
| C — Independent edits | Four simultaneous edits all converge | | |
| D — Same-object lock | Locked edits refused and reverted | | |
| E — Concurrent conflict | Conflicts detected, not merged | | |
| F — Disconnect | Presence updates, locks released | | |
| G — Reconnect | State restored, no stale overwrite | | |
| H — Server restart | Documented recovery behaviour | | |
| I — Five clients | Optional fifth client | | |
| Benchmark | Latency recorded, not guessed | | |

## If a test fails

1. Note which one, and what the agent and server consoles said.
2. Look it up in `docs/TROUBLESHOOTING.md`.
3. Re-run only the failing test after fixing.

Automated equivalents of Tests A, C, D, E, F and G run without any KiCad:

```powershell
python -m pytest tests\integration -q
```
