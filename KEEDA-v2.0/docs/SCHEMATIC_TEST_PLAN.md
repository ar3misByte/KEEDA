# Schematic Collaboration — Test Plan

Status recorded: **2026-09-18**, Windows 11, KiCad 10.0.6, Python 3.14.7.
Nothing is marked PASS that was not run and observed.

Automated portions:

```powershell
python -m pytest tests\unit\test_schematic.py -q
python -m pytest tests\integration\test_collab_integration.py -q -k schematic
python tests\manual\test_live_schematic.py        # needs the server running
```

---

## S0 — The feasibility finding this design rests on

**SETUP** eeschema open with a schematic; KiCad 10.0.6; `kicad-python` 0.8.0.

**ACTION** Send `GetOpenDocuments`, `GetItems` and `GetSelection` for a
`DOCTYPE_SCHEMATIC` document. Compare with the same calls for a PCB.

**EXPECTED** If eeschema implements the IPC API, schematic documents are
returned.

**ACTUAL**
```text
GetOpenDocuments(SCHEMATIC) -> "no handler available"
GetItems(SCHEMATIC)         -> "no handler available"
GetSelection(SCHEMATIC)     -> "no handler available"
GetOpenDocuments(PCB)       -> ['demo_board.kicad_pcb']
schematic_commands_pb2.py   -> 907 bytes (DESCRIPTOR only)
schematic_types_pb2         -> no symbol type at all
kipy.schematic              -> ImportError (PageSettings, BusEntryType)
```

**RESULT** **CONFIRMED: live schematic sync is impossible on KiCad 10.**
Collaboration is therefore detect/broadcast/lock/comment. See `docs/SCHEMATIC.md`.

---

## Phase S1 — Symbol movement and properties

### S1.1 — Parse a real schematic

**SETUP** `sample_project/demo_board.kicad_sch`.
**ACTION** `parse_schematic()`.
**EXPECTED** All six symbols with UUIDs, positions, values.
**ACTUAL** 38 objects; R1, R2, C1, C2, U1, J1 all present; R1 value `4k7`,
`lib_id` `Device:R`, footprint set.
**PASS**

### S1.2 — UUIDs are unique and stable

**ACTION** Parse twice; compare UUID sets.
**EXPECTED** Identical, no duplicates.
**ACTUAL** 105/105 unique on the larger `pic_programmer` demo; identical across
parses. Structural proof of stability: PCB footprints reference symbol UUIDs via
`(path "/<uuid>")`, so KiCad cannot renumber them.
**PASS**

### S1.3 — A save with no edit reports nothing

**ACTION** `diff_snapshots(parse(f), parse(f))`.
**EXPECTED** Empty.
**ACTUAL** `[]`.
**PASS** — formatting churn cannot masquerade as an edit.

### S1.4 — A value change is detected and described

**ACTION** Change R1 `4k7` -> `10k`; diff.
**EXPECTED** One `modify` on `value`.
**ACTUAL** `changed R1 value from 4k7 to 10k`.
**PASS**

### S1.5 — A move is detected

**ACTION** Change R1 position; diff.
**EXPECTED** One `modify` on `position`.
**ACTUAL** `moved symbol R1 to (99.00, 50.00)`.
**PASS**

### S1.6 — Rotation 0 and 360 are equal

**ACTUAL** No change reported.
**PASS**

---

## Phase S3 — Wires

### S3.1 — Wires parsed with endpoints and UUIDs

**ACTUAL** 197 wires parsed from `pic_programmer`; sample wire
`pts=[(87.63, 43.18), (87.63, 46.99)]` with a UUID.
**PASS**

### S3.2 — An endpoint change is detected

**ACTUAL** One `modify` on `end`.
**PASS**

---

## Phase S4 — Labels

### S4.1 — Local, global and hierarchical labels parsed

**ACTUAL** 31 local + 4 hierarchical labels from `pic_programmer`; sample
`DATA-RB7` at `(152.4, 80.01)` with a UUID.
**PASS**

---

## Phase S5 — Hierarchical sheets

### S5.1 — Child sheets are followed

**SETUP** `pic_programmer` (root + `pic_sockets` sub-sheet).
**ACTION** `parse_schematic(root)`.
**EXPECTED** Objects from both sheets, tagged by sheet.
**ACTUAL** 404 objects; sheets `['/', '/pic_sockets']`; 124 symbols.
**PASS**

### S5.2 — Path traversal is refused

**SETUP** A sheet whose `Sheetfile` is `../../outside.kicad_sch`.
**EXPECTED** The outside file is never read.
**ACTUAL** Refused and logged; the sheet symbol itself is still recorded.
**PASS**

---

## Phase S6 — End to end, live

### S6.1 — An edit reaches another client

**SETUP** Server running; agent on the project; a second client connected.
**ACTION** Change R1 `4k7` -> `10k` in `demo_board.kicad_sch` and save.
**EXPECTED** The second client receives a schematic change for R1.
**ACTUAL** Received in **0.80 s**, `domain: schematic`, `value -> 10k`.
**PASS**

### S6.2 — It lands in the activity timeline

**ACTUAL** `changed R1 value from 4k7 to 10k`, attributed to Designer A.
**PASS**

### S6.3 — KiCad Live never writes the schematic

**ACTION** After the round trip, re-read the file.
**EXPECTED** The author's edit is intact and untouched.
**ACTUAL** File still holds `10k`; no write by KiCad Live.
**PASS** — the whole codebase opens `.kicad_sch` read-only.

### S6.4 — A second edit is also detected

**ACTUAL** `R1 -> 2k2` detected.
**PASS** — no stale-baseline problem after the first change.

### S6.5 — The agent survives the session

**ACTUAL** Alive at the end; sample schematic restored by the test.
**PASS**

---

## Phase S7 — Schematic locks

### S7.1 — A locked symbol refuses another user's edit

**ACTION** A locks `schematic/R1`; B edits R1 and saves.
**EXPECTED** B is refused and told who holds it.
**ACTUAL** `lock_denied`, `owner_name: Aditya`, `domain: schematic`; the change
is not broadcast.
**PASS**

### S7.2 — Schematic and PCB locks are independent

**ACTION** A locks `schematic/R1`; B locks `pcb/R1`.
**EXPECTED** Both granted.
**ACTUAL** Both granted.
**PASS** — two people may work on a symbol and its footprint at once.

### S7.3 — Disconnect releases schematic locks

**ACTUAL** `LOCK RELEASED (disconnect) schematic/R1` in the server log.
**PASS**

---

## Results

| ID | Test | Result |
|---|---|---|
| S0 | Schematic IPC feasibility | CONFIRMED UNAVAILABLE |
| S1.1–S1.6 | Symbols: parse, UUIDs, no-op, value, move, rotation | PASS |
| S3.1–S3.2 | Wires | PASS |
| S4.1 | Labels | PASS |
| S5.1–S5.2 | Hierarchy and traversal safety | PASS |
| S6.1–S6.5 | Live end to end | PASS |
| S7.1–S7.3 | Schematic locks | PASS |

`tests/manual/test_live_schematic.py`: **14/14 PASSED**.

## Explicitly unsupported

Marked unsupported rather than implemented unreliably:

* Applying a schematic change to a running eeschema — **impossible**, see S0.
* Schematic selection presence — `GetSelection` has no schematic handler.
* Buses, bus entries, no-connect markers — not tracked.
* Detecting unsaved edits — detection is save-driven by necessity.

## Not run

* Five separate computers (hardware not available during development) —
  procedure in `docs/FIVE_COMPUTER_TEST.md`.
* A human dragging a symbol in the eeschema GUI. The automated test edits and
  saves the file, which is what eeschema does on Ctrl+S, but it is not a mouse.
