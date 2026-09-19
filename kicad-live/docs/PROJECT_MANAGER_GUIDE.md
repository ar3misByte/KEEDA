# KiCad Live — Project Manager Guide

Written for whoever is running the session: the person watching the dashboard
while four designers work. You do not need KiCad installed to use any of this.

---

## 1. Starting a project

A "project" is just a name that every designer shares. By default it is taken
from the board's filename, so everyone who opens `demo_board.kicad_pcb` lands in
the project `demo_board` automatically.

On the server machine:

```powershell
python -m server.main
```

To analyse a project that is not the shipped sample, point the server at it:

```powershell
$env:KICADLIVE_PROJECT_DIR = "D:\projects\my_board"
python -m server.main
```

That directory is what the **Hardware Summary** and **Schematic** pages read.

Then open the dashboard:

```text
http://SERVER_IP:8000/
```

To watch a different project: `http://SERVER_IP:8000/?project=my_board`

---

## 2. Adding users

There is no sign-up. A designer appears the moment they start their agent:

```powershell
python -m agent.main --server SERVER_IP --name "Designer A"
```

Whatever they pass as `--name` is what you see. Their identity persists between
sessions via a stored client id, so "Designer A" reconnecting is recognised as
the same person and their read markers carry over.

### Roles

Three roles exist: `manager`, `designer`, `viewer`. The dashboard connects as a
manager; agents connect as designers by default. A designer can be started
read-only, which is the practical equivalent of a viewer:

```powershell
python -m agent.main --server SERVER_IP --name "Reviewer" --read-only
```

**Be clear-eyed about this:** roles are advisory labels, not security. There is
no authentication — anyone who can reach port 8000 can connect as anyone. This
is a LAN prototype; do not run it on an untrusted network.

---

## 3. Monitoring activity

**Overview** is the page to leave on the projector. It shows designers online,
changes today, active locks, open comments, conflicts and the project version,
with a live activity feed underneath.

**Activity** is the full timeline, filterable by user, domain and action. Click
any entry to open the **change inspector**:

```text
CHANGE DETAILS
  User          Aditya
  Time          18/09/2026, 17:42:31
  Domain        Schematic
  Object        R1
  Action        modified
  Property      value
  Before        4k7
  After         10k
```

For a move it shows coordinates rather than raw internal data:

```text
  Before        X = 42.5   Y = 31.2
  After         X = 50.0   Y = 31.2
```

Colour coding in the feed: red = conflict, amber = someone blocked by a lock,
blue = comment.

---

## 4. Interpreting locks

The Overview lock panel shows what is claimed right now:

```text
SCH   R1   ->  Aditya    118s left
PCB   U2   ->  Rahul      94s left
```

Read it like this:

* **Locks are advisory.** They are honoured by the KiCad Live agent, not
  enforced inside KiCad. Someone determined can still edit a locked part; the
  system will refuse to share the change and say so.
* **A designer takes a lock by selecting a component** in the PCB editor. They
  release it by deselecting. Nobody presses a "lock" button.
* **Schematic and PCB locks are separate.** `SCH R1` and `PCB R1` are different
  claims, and that is deliberate — two people can legitimately work on a
  component's symbol and its footprint at once.
* **A lock expires after 120 seconds** without refresh, and disconnecting
  releases every lock that client held, immediately.

If a lock looks stuck, it belongs to someone still connected but idle. Wait for
the countdown, or ask them to deselect.

---

## 5. Inspecting conflicts

A conflict means two designers changed the **same field of the same object**
from the same starting point. The dashboard shows it in red:

```text
17:43:02  Rahul  CONFLICT on R1.position — also changed by Aditya
```

The losing designer's board is reverted to the server's value and they are told
what happened. **Nothing is merged automatically** — there is no correct answer
a computer can pick for "where should this part go".

What to do as manager: ask the two people involved which value is right. The
one who should win simply makes the change again; that is a fresh edit from the
current version and will be accepted.

Conflicts are rare in practice because selecting a component locks it. A
conflict usually means two people edited without selecting first.

---

## 6. Reading and resolving comments

**Comments** is a review tool, not a chat. Every comment is attached to a real
object — a schematic symbol or a PCB footprint.

To comment: pick the domain, type the reference (`R1`, `U1`…) — the field
autocompletes from the actual project — and write the note. You can also click
any row on the **Schematic** or **PCB** page to comment on it directly.

A thread looks like this:

```text
SCH  R1                                              OPEN
  Aditya   17:45   "Should this resistor be 4.7k instead of 10k?"
  Rahul    17:47   "Yes. This is for the I2C pull-up."
  [Reply...]                                      [Resolve]
```

Replies are one level deep, which keeps a review conversation readable.

**Resolving** marks the thread done and drops it out of the open count. Anyone
can resolve, and anyone can reopen — this is a small team tool, not a workflow
engine. Resolution is recorded in the activity timeline with who did it.

Filter with **All / Open / Resolved / Mine**. "Mine" means threads you started
*or replied to*.

The sidebar badge shows the open-thread count, so unresolved review points stay
visible.

---

## 7. Reading the hardware summary

**Hardware Summary** is generated on the server from the KiCad files. It uses
**no internet connection and no AI service** — connectivity comes from KiCad's
own `kicad-cli` netlist exporter, and the board facts from parsing the
`.kicad_pcb`.

```text
Microcontroller   Not available
                    evidence: no recognised MCU part number in the schematic
Power rails       +3V3
Interfaces        I2C
                    evidence: net /SDA, net /SCL
Components        6 components, 5 distinct values
Board stack-up    2-layer
Board size        90.0 x 35.0 mm
                    evidence: measured from the Edge.Cuts outline
```

**The important habit: read the evidence line.** Every claim states what in the
project supports it. If something cannot be determined, it says
**Not available** and explains why — it is never filled in with a plausible
guess.

An interface is only listed when the schematic actually names those signals or
their pin functions. A part being *capable* of USB is not evidence that the
board uses USB.

If the summary looks wrong, it is telling you something true about the project:
missing footprints, no board outline, or nets that are not named.

---

## 8. Version history

Every accepted change gets a version number, attributed and timestamped. The
Overview tile shows the current version; `GET /api/versions` returns the list.

Versions survive a server restart because history is persisted to SQLite. After
a restart, numbering **continues** rather than resetting, so a version number
always refers to one specific change.

---

## 9. Seeing what changed while you were away

When a designer reconnects, the server sends them a **What changed since your
last session** summary, computed before their own arrival is recorded:

```text
Since your last session:
  3 schematic changes
  5 PCB changes
  2 comments

  Rahul   modified U1
  Sarah   moved J4 on the PCB
```

It deliberately excludes their own edits — "what changed" means what other
people did. Marking it read moves their marker to the latest event.

As manager you can check anyone's view:

```text
GET /api/whats-new?project=demo_board&user=<client_id>
```

---

## 10. What survives a restart

| Survives | Does not survive |
|---|---|
| Comments and replies | Locks |
| Activity timeline | Presence / who is online |
| Version history | In-memory board state (re-seeded by the first agent back) |
| Users, roles, sessions | |
| Read markers | |

Locks and presence are deliberately not persisted: both describe who is holding
something *right now*, and restoring them would resurrect locks owned by nobody.

Everything persisted lives in `data/kicadlive.db` on the server. It is a single
SQLite file — copy it to back up a session.

---

## 11. Where the data lives

All project data stays on the machine running the server:

* KiCad files — on each designer's own computer, never uploaded
* Comments, activity, usernames, versions — `data/kicadlive.db` on the server
* Hardware summaries — computed on demand, cached in memory for 30 seconds

Nothing is transmitted to any external service. The dashboard talks only to
your server.

---

## 12. Quick reference

| Task | Where |
|---|---|
| Who is online, what are they doing | Overview |
| Everything that has happened | Activity |
| Details of one change | Click any activity row |
| Review discussions | Comments |
| What the board actually is | Hardware Summary |
| Symbols, sheets, schematic locks | Schematic |
| Footprints, positions, PCB locks | PCB |
| Server health | `http://SERVER_IP:8000/health` |

Troubleshooting for every failure mode: `docs/TROUBLESHOOTING.md`.
