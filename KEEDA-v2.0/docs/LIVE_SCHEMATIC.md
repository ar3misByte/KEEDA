# KEEDA v2.0 - near-live schematic sync (and file-only layout sync)

## The problem this solves

* eeschema has **no live API** in KiCad 10, so another user's schematic edit
  can never be *pushed* into an open schematic editor as an object.
* On some computers KiCad's **PCB API answers "busy" or "timed out" for ever**
  (`KiCad is busy and cannot respond...`), so layout sync never starts.

## What v2.0 does instead

It automates exactly what a person would do by hand, in a few seconds:

```
author edits -> editor auto-saved -> file uploaded -> server notifies everyone
   -> each agent MERGES the file into its own -> editor reloaded (File > Revert)
```

| Step | Who | How | Typical time |
|---|---|---|---|
| Auto-save | author's agent | posts the editor's *File > Save* menu command once an edit has been unsaved for 3 s | 3 s (`--autosave-delay`) |
| Share | author's agent | uploads the saved `.kicad_sch` (and `.kicad_pcb` in file-only mode) | < 1 s |
| Notify | server | broadcasts the new file list | instant |
| Merge | each teammate's agent | 3-way merge by object UUID (`common/sexpr_merge.py`) | ms |
| Reload | each teammate's agent | posts *File > Revert* and confirms the "Yes" prompt | ~1 s |

Measured on this repository's test rig (one Windows 11 machine, real eeschema
10.0.6, real server): file saved by one agent -> teammate's file updated in
**0.4 s** -> teammate's open schematic editor showing the new value within
about 1 s more. Author side: an edit in a real eeschema was auto-saved and
reached the other agent's file within seconds. **A real multi-computer LAN run
has not been done.**

### Honest limits

* An edit is invisible to others until the author's editor **saves** it (the
  edit only exists in that editor's memory). Auto-save keeps the gap at ~3 s.
  This is "near-live", not keystroke-live. No tool can do better on KiCad 10.
* Auto-save / auto-reload need **Windows** and an **English KiCad UI** (menu
  items are found by the text "Save" / "Revert"). Otherwise the agent prints
  "File > Revert" instructions instead and everything else still works.
* The reloaded editor loses its undo history and may reset the view.
* Two people changing the **same object** at once cannot be merged: the
  teammate's copy goes to `.kicad_live/incoming/`, your file is untouched, and
  you are told. Different objects (the normal case) merge automatically.
* If someone types into the editor in the ~100 ms between the agent's check and
  the reload, that edit could be lost. The agent checks the `*` (unsaved) mark
  immediately before every reload and never reverts over unsaved edits.

## Modes

| Mode | When | PCB | Schematic |
|---|---|---|---|
| **LIVE** | KiCad API works | per-move via API (sub-second) | file share + merge + auto save/reload |
| **FILE-ONLY** | `--file-only`, or the API is busy / times out for 30 s at start-up (automatic) | file share + merge + auto save/reload | same |

If one computer needs FILE-ONLY for the layout, run **every** computer with
`--file-only` so all of them use the same layout mechanism (mixing them means
PCB edits made through the API do not reach file-only computers).

## Running it

Server (one computer):

```
pip install -r requirements.txt
python -m server.main
```

Each user computer - start the agent **inside the KiCad project folder** (the
one containing the `.kicad_sch`/`.kicad_pcb`), with KiCad's editors open:

```
cd C:\path\to\project
python -m agent.main --server SERVER_IP --name "Designer A"           # automatic mode
python -m agent.main --server SERVER_IP --name "Designer A" --file-only   # force file-only
```

The banner must show the **same `Project` on every computer** (taken from the
`.kicad_pcb` file name; override with `--project NAME`). Start every computer
from identical copies of the project the first time.

Useful flags: `--no-auto-save`, `--no-auto-reload`, `--autosave-delay 3`,
`--schematic-sync-interval 30` (fallback poll; new saves are pushed
immediately), `--sync-pcb-files`, `--read-only`.

## Safety guarantees (all covered by tests)

* Compared by sha256 against the last version both sides agreed on.
* Old file backed up to `.kicad_live/backup/` before any overwrite.
* Atomic writes (temp file + rename); half-written saves are ignored.
* An editor with unsaved edits is saved first (auto-save) or left alone.
* A push carries its base version; the server rejects stale pushes, so nobody
  silently overwrites a teammate.
* File names are validated on the server (no paths, only `.kicad_sch/.kicad_pcb`).

## Tests

`tests/unit/test_sexpr_merge.py` (merge rules, real sample files),
`tests/unit/test_schematic_sync.py` (two agents through the real file store,
merge, conflicts, editor automation with a fake editor, PCB files),
`tests/integration/test_schematic_files_integration.py` (real server, real
WebSockets). The Windows automation (`agent/win_ui.py`) was verified by hand on
real KiCad 10.0.6 editors, not by an automated test.
