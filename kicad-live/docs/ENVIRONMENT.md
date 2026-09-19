# KiCad Live — Environment Verification (Milestone 0)

Everything in this document was **executed on the development machine** and the
output pasted back. Nothing here is assumed. Where a version is stated, it is
the version the system was actually tested with.

| Component | Tested version | How it is used |
|---|---|---|
| OS | Windows 11 Pro (10.0.26200) | Dev + demo machines |
| KiCad | **10.0.6** | Unmodified; runs the IPC API server |
| KiCad bundled Python | 3.11.5 (`bin/python.exe`) | Runs `pcbnew` scripts + the Action Plugin |
| System Python | 3.14.7 | Runs the server, the agent and the tests |
| `kicad-python` (`kipy`) | 0.8.0 | Talks to a *running* KiCad over IPC |
| Git | 2.55.0 | Optional; version-history backend |

> KiCad **9.0 or newer is required** — the IPC API that this project is built on
> does not exist in KiCad 8 or earlier. Tested on 10.0.6.

---

## 0.1 Verify Python

```powershell
python --version
```

Expected: `Python 3.11` or newer. (Tested with 3.14.7.)

## 0.2 Verify KiCad and its bundled Python

```powershell
& "C:\Program Files\KiCad\10.0\bin\python.exe" --version
```

Observed:

```text
Python 3.11.5
```

## 0.3 Verify the `pcbnew` scripting API

```powershell
& "C:\Program Files\KiCad\10.0\bin\python.exe" -c "import pcbnew; print(pcbnew.GetBuildVersion()); print(hasattr(pcbnew,'ActionPlugin'))"
```

Observed:

```text
10.0.6
True
```

This proves both the file-level API **and** Action Plugin support are present.

## 0.4 Enable the KiCad IPC API server

This is the single most important setup step. **KiCad must be closed** while you
change it, because KiCad rewrites its config on exit.

In the KiCad GUI: **Preferences → Plugins → check "Enable KiCad API"**, then
restart KiCad.

Equivalent scripted check (read-only):

```powershell
Select-String -Path "$env:APPDATA\kicad\10.0\kicad_common.json" -Pattern "enable_server" -Context 1,1
```

Expected once enabled:

```text
"enable_server": true
```

The repo ships a helper that sets it for you (close KiCad first):

```powershell
python tools/enable_kicad_api.py
```

## 0.5 Install the Python dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 0.6 Verify the live IPC link (KiCad must be OPEN with a board)

```powershell
python tools/check_env.py
```

Observed on the dev machine:

```text
[PASS] kipy importable                 - kicad-python 0.8.0
[PASS] Connected to KiCad              - 10.0.6
[PASS] API version                     - 10.0.6
[PASS] Board open                      - demo_board.kicad_pcb
[PASS] Footprints readable             - 6 footprints
[PASS] Board poll latency              - 1.1 ms average
```

## 0.7 Verify the file-level API round-trip (Milestone 1)

```powershell
& "C:\Program Files\KiCad\10.0\bin\python.exe" tests\manual\test_pcb_api.py
```

Observed:

```text
[PASS] pcbnew imported - build 10.0.6
[PASS] Board loaded
[PASS] Found 6 footprints
[PASS] R1 found
[PASS] R1 has a UUID - 33c18730-0af9-4ad4-972d-782bf1b87199
[PASS] Position changed - 60.000 -> 65.000 mm
[PASS] Board saved
[PASS] Board reloaded
[PASS] Modification persisted - R1.x = 65.000 mm
[PASS] UUID stable across save/reload - 33c18730-0af9-4ad4-972d-782bf1b87199
RESULT: all 11 checks PASSED
```

**Why this matters:** footprint UUIDs are stable across save/reload *and* the
UUID reported over IPC is byte-identical to the one in the `.kicad_pcb` file.
That single fact is what lets the diff engine name objects reliably.

## 0.8 Verify network reachability between computers

On the server machine:

```powershell
ipconfig | Select-String "IPv4"
```

On each client machine (replace with the server's address):

```powershell
Test-NetConnection -ComputerName 192.168.1.50 -Port 8000
```

Expected: `TcpTestSucceeded : True`.

---

## Verified API facts the implementation relies on

These were each confirmed by running code against KiCad 10.0.6:

1. `KiCad(client_name=...)` connects over a local socket; no TCP port involved.
2. `board.get_footprints()` returns objects with `.id.value` (UUID string),
   `.position` (nanometres), `.orientation.degrees`, `.layer`,
   `.reference_field.text.value`, `.value_field.text.value`.
3. `board.begin_commit()` / `update_items()` / `push_commit()` applies changes to
   the **live in-memory board**, and the result lands on KiCad's normal undo
   stack (Ctrl+Z reverts it).
4. `Vector2.from_xy(x_nm, y_nm)` is the correct constructor.
   `Vector2(x, y)` raises `TypeError` — it takes a protobuf, not two ints.
5. `board.get_selection()` returns the user's current selection — this is what
   drives presence and auto-locking.
6. Polling `get_footprints()` costs ~1.1 ms for a 6-footprint board, so a 250 ms
   poll loop is far below the noise floor.

### Known hazard: leaked commits

If a client calls `begin_commit()` and then dies before `push_commit()` or
`drop_commit()`, KiCad keeps that commit open **keyed by client name**, and every
later connection using the same client name fails with:

```text
KiCad returned error: the client <name> already has a commit in progress
```

Verified: this survives client reconnection and is only cleared by restarting
KiCad. The agent mitigates it two ways — every commit is wrapped in
`try/finally: drop_commit`, and each agent run uses a unique client name so a
crashed run can never block the next one. See `docs/TROUBLESHOOTING.md`.
