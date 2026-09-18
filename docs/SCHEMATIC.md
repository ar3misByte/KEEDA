# KiCad Live — Schematic Collaboration

## The short version

Schematic collaboration is **detect, broadcast, lock, comment and review**.
It is **not** live editing, and it never writes your schematic.

Moving a symbol in eeschema does **not** move it on anyone else's screen. When
you save, everyone is told what you changed, the change lands in the activity
timeline and the hardware summary, and the symbol can be locked and discussed.

This is a limitation of KiCad, not a design preference. The rest of this
document is the evidence.

---

## 1. Why live schematic sync is impossible on KiCad 10

The PCB half of KiCad Live drives a *running* pcbnew through KiCad's IPC API.
The obvious plan was to do the same for eeschema. It cannot be done, and this
was established by probing the running application rather than by reading docs.

### Probe 1 — eeschema serves no API at all

With eeschema open and a schematic loaded:

```text
GetOpenDocuments(DOCTYPE_SCHEMATIC) -> "no handler available for request of
                                        type kiapi.common.commands.GetOpenDocuments"
GetItems      (schematic document)  -> "no handler available"
GetSelection  (schematic document)  -> "no handler available"
```

The identical calls against a PCB document return real data. **Only pcbnew
implements the IPC API.**

### Probe 2 — the schematic command set is empty

Compiled protobuf sizes in `kicad-python` 0.8.0:

| File | Size |
|---|---|
| `proto/board/board_commands_pb2.py` | 15,858 bytes |
| `proto/schematic/schematic_commands_pb2.py` | **907 bytes** — only `DESCRIPTOR` |

There are no schematic commands defined. A client cannot even *form* a request.

### Probe 3 — there is no schematic symbol type

The shipped `schematic_types_pb2` contains only:

```text
Line, Text, LocalLabel, GlobalLabel, HierarchicalLabel, DirectiveLabel
```

No symbol type. Symbols — the thing designers actually move — are not
representable over the API.

### Probe 4 — `kipy.schematic` does not even import

```text
ImportError: cannot import name 'PageSettings' from 'kipy.common_types'
ImportError: cannot import name 'BusEntryType' from
             'kipy.proto.schematic.schematic_types_pb2'
```

`kipy.schematic` advertises `get_symbols()` and `get_netlist()`, but it is
written against a **future** KiCad. On KiCad 10.0.6 it cannot be loaded.

### Why we do not write the file instead

Writing `.kicad_sch` under a running eeschema was rejected deliberately:

* eeschema holds its own in-memory copy and will **overwrite your file** the
  next time the author presses Ctrl+S, silently discarding the change.
* A designer could lose work with no warning and no undo.

The brief itself says to mark a feature unsupported rather than risk corrupting
a schematic. That is exactly what this is.

**KiCad Live never opens a schematic file for writing.** The only schematic
access in the entire codebase is `parse_file()`, which is read-only.

---

## 2. What schematic collaboration does instead

```text
Designer A edits R1 in eeschema and presses Ctrl+S
        |
        v
  .kicad_sch mtime changes
        |
        | agent re-reads and diffs (~17 ms for a 229 KB schematic)
        v
  structured change: R1.value 4k7 -> 10k
        |
        v
  Sync server: record event, bump version, broadcast
        |
        +---> other agents      "Designer A changed R1 value ... review it"
        +---> dashboard         activity timeline, schematic view
        +---> hardware summary  refreshed
        +---> locks / comments  attach to R1 by UUID
```

Measured on the development machine: **an edit is detected and delivered to
another client in 0.80 s** (`tests/manual/test_live_schematic.py`).

### What is tracked

| Object | Fields |
|---|---|
| Symbol | reference, value, lib_id, position, rotation, mirror, unit, dnp, footprint |
| Wire | start, end |
| Label (local / global / hierarchical) | text, position, rotation |
| Junction | position |
| Hierarchical sheet | sheet name, sheet file, position |

Hierarchical projects are traversed from the root sheet. Traversal is bounded
(64 sheets) and refuses any sheet file that resolves outside the project
directory, so a crafted `Sheetfile` cannot read arbitrary files.

### Identity

Everything is keyed on KiCad's own UUIDs, which are stable across saves. That
is not an assumption: a PCB footprint references its schematic symbol through
`(path "/<uuid>")`, so KiCad cannot renumber symbol UUIDs without breaking its
own schematic-to-board link.

### Noise

A save that changes nothing produces **zero** reported changes — verified by
`test_reparse_produces_no_diff`. Only the tracked fields above are compared, so
formatting churn never looks like an edit.

---

## 2b. Workaround: schematic FILE sharing on a timer

Objects cannot be pushed into a running eeschema, but **files can be
exchanged**. Each agent now shares saved sheets through the server:

1. You save the schematic (Ctrl+S). Within about a second the agent uploads
   the `.kicad_sch` to the server.
2. Every **2 minutes** (`--schematic-sync-interval 120`, `0` turns it off) and
   when it connects, each teammate's agent downloads newer sheets and writes
   them into **their own project folder** (atomically).
3. The teammate reloads the sheet in eeschema (**File > Revert**, or close and
   reopen it - the exact menu name depends on your KiCad build).

So a schematic edit reaches a teammate's disk within the interval and their
screen when they reload. It is not live, and no tool can make it live.

**Nothing is overwritten silently**

| Situation | What happens |
|---|---|
| Your sheet is unchanged since the last sync | Replaced by the team's; the old file is backed up to `.kicad_live/backup/` |
| First time you join and your copy differs | The team's copy is adopted; yours is backed up |
| You AND a teammate both edited the sheet | Your file is left alone; theirs goes to `.kicad_live/incoming/`; a banner tells you. Merge by hand and save; your merged save is then shared |
| You save an old version after a teammate's newer one | The server rejects the upload (stale base); you are told |
| A save is caught half-written | Ignored until it is complete |

**Rule of thumb:** after a "written to your project folder" banner, reload the
sheet *before* you edit or save. Saving the old in-memory copy is treated as a
conflicting edit.

Server copies live in `data/schematics/<project>/` and survive restarts.
Unit tests: `tests/unit/test_schematic_sync.py`; server integration:
`tests/integration/test_schematic_files_integration.py`. What has NOT been
verified: reloading via the eeschema menu on a real KiCad, and a real
multi-computer run.

## 3. Schematic locks

Locks are keyed by `(domain, object id)`, so schematic and PCB locks are
independent. Two designers may legitimately work on R1's symbol and R1's
footprint at the same time.

```text
Aditya selects R1 in the schematic  -> lock: schematic/R1 -> Aditya
Rahul  edits R1 and saves           -> refused, and Rahul is told who holds it
Rahul  moves R1's FOOTPRINT         -> allowed; different domain
```

Because there is no write path, a schematic lock cannot *revert* someone's
edit the way a PCB lock does. Instead the edit is **not shared**, and the author
is told plainly:

```text
>>> R1 is locked by Aditya in the schematic. Your edit was NOT shared -
    coordinate before saving again.
```

That is an honest description of what a soft lock can do here.

---

## 4. Running it

The agent watches the schematic in the project directory it is given:

```powershell
python -m agent.main --server SERVER_IP --name "Designer A" `
                    --project-dir D:\path\to\your\project
```

`--project-dir` defaults to the current directory, which is the project folder
in the documented workflow. Turn the feature off with `--no-schematic`.

On startup the agent reports what it found:

```text
 Schematic  demo_board.kicad_sch (38 objects, 1 sheet(s)) - review only
```

If it says `not found`, point `--project-dir` at the folder holding your
`.kicad_sch`.

---

## 5. Limitations, stated plainly

* **Schematic changes are never applied to anyone's eeschema.** They are
  reported.
* **Detection requires a save.** Unsaved edits are invisible, unlike the PCB
  side which is live.
* **Presence cannot show a schematic selection.** `GetSelection` has no handler
  for schematic documents, so KiCad Live cannot see what you have clicked in
  eeschema. PCB selection *is* visible.
* **No conflict resolution.** Two people editing the same symbol both get
  reported; the lock is what prevents the clash, not a merge.
* **Buses, bus entries and no-connect markers are not tracked.**

## 6. If a future KiCad adds a schematic API

The code is arranged so this is a contained change:

* `agent/schematic_link.py` is the only file that reads schematic state.
* `server/schematic/` holds the object model and diff, already separate from
  the PCB engine.
* The protocol already carries a `domain` field on every change and lock.

Adding live schematic sync would mean giving `SchematicLink` an `apply_changes`
method and letting `_on_remote_change` route schematic changes to it — the same
shape the PCB path already has.
