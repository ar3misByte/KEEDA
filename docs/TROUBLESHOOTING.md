# KiCad Live — Troubleshooting

Every entry below is a failure that actually happened while building or testing
this project. Each gives SYMPTOM / CAUSE / CHECK / FIX / VERIFY.

**Start here.** These three commands identify most problems in under a minute:

```powershell
python tools\check_env.py --server SERVER_IP     # is this machine ready?
curl http://SERVER_IP:8000/health                # is the server alive?
python tools\enable_kicad_api.py --check         # is the KiCad API on?
```

---

## 1. The server does not start

**SYMPTOM**
`python -m server.main` exits immediately, or prints
`[Errno 10048] error while attempting to bind on address`.

**CAUSE**
Port 8000 is already taken — usually by a server you forgot to stop.

**CHECK**
```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -like '*server.main*' } |
  Select-Object ProcessId, CommandLine
```

**FIX**
Stop the old server, or use another port.
```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -like '*server.main*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

python -m server.main --port 8001
```

**VERIFY**
`curl http://127.0.0.1:8000/health` returns `{"status":"ok",...}`.

---

## 2. `ModuleNotFoundError: No module named 'fastapi'` (or `kipy`, `websockets`)

**SYMPTOM**
Any command fails on import.

**CAUSE**
The virtual environment is not active, or dependencies were never installed.

**CHECK**
```powershell
python -c "import sys; print(sys.executable)"
```
The path should be inside your project's `.venv`.

**FIX**
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
If activation is blocked:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

**VERIFY**
`python -c "import fastapi, websockets, kipy; print('ok')"`

---

## 3. A client cannot connect to the server

**SYMPTOM**
The agent prints `connection lost` / `reconnecting in 1s` for ever, or the
browser cannot open `http://SERVER_IP:8000/health`.

**CAUSE**
Wrong IP, the firewall, or the machines are on different networks.

**CHECK**
On the client:
```powershell
Test-NetConnection -ComputerName SERVER_IP -Port 8000
```
* `PingSucceeded: False` **and** `TcpTestSucceeded: False` → not the same network.
* `PingSucceeded: True` **but** `TcpTestSucceeded: False` → firewall.

On the server, confirm the address and that it is listening:
```powershell
ipconfig | Select-String "IPv4"
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

**FIX**
1. Use the IP on the same subnet as the clients. Ignore `169.254.*` addresses
   and VirtualBox/WSL/Hyper-V adapters.
2. Add the firewall rule, in an **Administrator** PowerShell on the server:
   ```powershell
   New-NetFirewallRule -DisplayName "KiCad Live" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
   ```
3. Make sure the server was not started with `--host 127.0.0.1`, which accepts
   local connections only. The default `0.0.0.0` is what you want.
4. On guest/corporate Wi-Fi, "client isolation" can block machine-to-machine
   traffic entirely. Use a phone hotspot or a switch instead.

**VERIFY**
`http://SERVER_IP:8000/health` loads from a *different* computer.

---

## 4. The agent cannot connect to KiCad

**SYMPTOM**
```text
[FAIL] Could not connect to KiCad.
       could not reach KiCad (...). Is KiCad running with a PCB open, and is
       'Enable KiCad API' ticked in Preferences > Plugins?
```

**CAUSE** (in order of likelihood)
1. The API is not enabled.
2. It was enabled but KiCad was not restarted.
3. KiCad is not running, or has no **PCB** open (the schematic editor is not enough).
4. KiCad 8 or older — the IPC API does not exist there.
5. The setting was changed while KiCad was open, so KiCad overwrote it on exit.

**CHECK**
```powershell
python tools\enable_kicad_api.py --check
Get-Process pcbnew, kicad -ErrorAction SilentlyContinue
```
And confirm the version in **Help → About KiCad** is 9.0 or newer.

**FIX**
1. Close KiCad **completely**.
2. `python tools\enable_kicad_api.py`
3. Start KiCad and open a PCB.
4. `python tools\check_env.py`

**VERIFY**
`[PASS] Connected to KiCad - 10.0.6`

---

## 5. `the client <name> already has a commit in progress`

**SYMPTOM**
The agent or a tool fails with that error and never recovers.

**CAUSE**
A client began a KiCad commit and died before finishing it. KiCad tracks open
commits **per client name** and keeps that commit open. Verified behaviour: it
survives reconnection and is only cleared by restarting KiCad.

**CHECK**
The error message names the client. If it starts with `kicad-live-`, it came
from this project.

**FIX**
Restart KiCad. That is the only way to clear a stuck commit.

This should not recur: the agent wraps every commit in `try/finally` with
`drop_commit`, and each run uses a **unique client name**, so a crashed run can
never block the next one. If you see it repeatedly, file it — something is
crashing between `begin_commit` and `push_commit`.

**VERIFY**
`python tools\check_env.py` reaches `[PASS] Board is writable`.

---

## 6. The plugin does not appear in KiCad

**SYMPTOM**
**Tools → External Plugins** has no "KiCad Live" entry.

**CAUSE**
Not installed, installed into another KiCad version's folder, KiCad not
restarted, or the plugin raised on import.

**CHECK**
```powershell
python tools\install_plugin.py --list
Get-Content "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins\kicad_live\last_load.json"
```
`last_load.json` is written every time KiCad imports the plugin:
* `"status": "loaded"` → it imported fine; you are looking in the wrong menu.
* `"status": "failed"` → `detail` holds the traceback.
* file missing → KiCad never imported it at all.

**FIX**
```powershell
python tools\install_plugin.py
```
then **restart KiCad**. KiCad only scans for plugins at startup. If several
KiCad versions are installed, the installer writes to all of them; make sure
you restarted the one you are actually using.

**VERIFY**
```powershell
& "C:\Program Files\KiCad\10.0\bin\python.exe" tests\manual\test_plugin_loads.py
```
then check **Tools → External Plugins**.

---

## 7. The plugin appears but does nothing / shows an error

**SYMPTOM**
Clicking the menu entry shows "KiCad Live is not configured" or
"The Python interpreter recorded for KiCad Live no longer exists".

**CAUSE**
The plugin is a launcher. It stores the repository path and the interpreter in
`kicad_live_config.json` at install time. Moving or renaming the repository, or
deleting `.venv`, invalidates it.

**CHECK**
```powershell
Get-Content "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins\kicad_live\kicad_live_config.json"
```

**FIX**
Re-run `python tools\install_plugin.py` from the repository's current location.

**VERIFY**
The dialog opens and the agent starts in a new console window.

---

## 8. Changes are not synchronising

**SYMPTOM**
Two agents are connected but moving a part on one does nothing on the other.

**CAUSE** (in order)
1. The two machines are in **different projects**.
2. The boards have **different UUIDs** (they were not copied from the same file).
3. The component is locked by someone else.
4. The change conflicted.

**CHECK**
Compare the `Project` line printed by each agent — they must match exactly.

Then compare UUIDs across machines:
```powershell
python -c "from agent.kicad_link import KiCadLink; l=KiCadLink(); l.connect(); [print(v['reference'], v['uuid']) for v in sorted(l.read_snapshot().values(), key=lambda s: s['reference'])]"
```
The UUID for R1 must be **identical** on every machine.

Watch the agent console. It logs `LOCAL <summary>` when it sends and
`REMOTE <summary>` when it applies.

**FIX**
* Different projects → pass the same `--project demo_board` on every machine.
* Different UUIDs → copy one machine's `sample_project/demo_board.kicad_pcb` to
  all the others and reopen it. UUIDs are how objects are matched; boards that
  merely *look* the same will not sync.
* Locked → the console says so by name; wait or ask them to deselect.
* Conflict → the console prints a CONFLICT block. See entry 11.

**VERIFY**
Move R1 and watch the other machine's console print `REMOTE moved R1 ...`.

---

## 9. An infinite synchronisation loop

**SYMPTOM**
A component jitters continuously, or the logs show endless changes for one part.

**CAUSE**
This is the failure the design specifically prevents, so seeing it means a bug.
Two guards exist: the baseline is updated to the expected post-state *before*
each remote write, and a one-poll-cycle echo mark backs that up.

**CHECK**
Stop every agent but one. If the jitter stops, it was a loop between two agents.
Run with `--verbose` and look for repeated `LOCAL`/`REMOTE` lines on one UUID.

**FIX**
Stop all agents, restart the server (`Ctrl+C`, then start again), then start the
agents one at a time. If it recurs, capture `--verbose` logs from both agents —
it is a genuine defect.

**VERIFY**
`tests/manual/test_live_sync.py` includes a test that asserts no spurious
traffic once idle, and `tests/unit/test_echo_suppression.py` covers the logic.

---

## 10. Two clients show different board states

**SYMPTOM**
R1 is in different places on two machines and stays that way.

**CAUSE**
One agent was disconnected while the other kept working, or a change was
refused and the board drifted.

**CHECK**
Compare the dashboard's **Version** number with what each agent believes. Check
the server's authoritative value:
```powershell
curl http://SERVER_IP:8000/api/status
```

**FIX**
Restart the out-of-date agent. On connect it receives the full `project_state`
and **adopts the server's values**, applying whatever is needed to its board.
Reconnection is deliberately one-directional: a returning client can never
overwrite work done while it was away.

**VERIFY**
The agent logs `adopting N field(s) from the server`, and the boards match.

---

## 11. A conflict was reported

**SYMPTOM**
```text
==============================================================
  CONFLICT on R1.position
    you changed it to : {'x': 40.0, 'y': 35.0}
    Aditya changed it to : {'x': 45.0, 'y': 30.0}
    your base v7, server is at v8
  KiCad Live will not guess. Reverting to the server's value.
  To keep YOUR value instead, move R1 again now.
==============================================================
```

**CAUSE**
Two people changed the same field of the same component from the same starting
point. This is working as designed — the system detects it and refuses to merge.

**CHECK**
The message names the other person and both values.

**FIX**
Your board is put back to the server's value so the two do not silently
diverge. If your value was the right one, simply move the part again — that is
a fresh change from the current version and will be accepted.

To avoid conflicts entirely, **select a component before editing it**; selecting
it takes a soft lock automatically.

**VERIFY**
The dashboard's Activity feed shows the conflict, then your new change.

---

## 12. A lock is stuck

**SYMPTOM**
The dashboard shows a component locked by someone who has left.

**CAUSE**
Normally impossible — disconnecting releases all of a client's locks
immediately. A lock can linger only if a client is still connected but idle.

**CHECK**
The dashboard shows a countdown next to each lock.

**FIX**
Wait. Locks expire after **120 seconds** without refresh, and the server's
janitor reclaims them within 5 seconds of expiry. To release immediately, the
owner deselects the component or stops their agent.

**VERIFY**
The lock disappears from the dashboard and someone else can select the part.

---

## 13. A WebSocket keeps disconnecting

**SYMPTOM**
The agent cycles between `connected` and `connection lost`.

**CAUSE**
Flaky Wi-Fi, the machine sleeping, or the server being restarted.

**CHECK**
The agent's reconnect backoff (1, 2, 3, 5, 8, 10 s) is visible in its log. A
client silent for 30 s is dropped by the server and will reconnect on its own.

**FIX**
This is handled automatically — the agent reconnects for ever and resyncs from
`project_state` each time. Nothing is lost. For a stable demo prefer wired
Ethernet, and disable sleep:
```powershell
powercfg /change standby-timeout-ac 0
```

**VERIFY**
The dashboard shows the designer back online with the correct state.

---

## 14. The server crashed or was restarted

**SYMPTOM**
Every agent reports the connection lost at once.

**CAUSE**
The server process ended.

**CHECK**
Look at the server console for a traceback.

**FIX**
Start it again with the same command. Agents reconnect by themselves within
about ten seconds. Documented recovery behaviour:

* Object state is held **in memory** and is lost on restart.
* Change **history is persisted** to `data/<project>.history.jsonl`, and version
  numbering continues from where it left off, so versions never go backwards.
* The first agent to reconnect **re-seeds** the project from its own board.
* Locks are not persisted; everyone starts unlocked.

Because state is reseeded from a live board, the important thing after a server
restart is that the agents are all still running — they are the backup.

**VERIFY**
`curl http://SERVER_IP:8000/health` is ok, the dashboard repopulates, and a test
move propagates.

---

## 15. KiCad says the board has changed on disk

**SYMPTOM**
A "file has changed on disk / reload?" dialog appears.

**CAUSE**
**KiCad Live never writes your board file**, so this did not come from us. It
means something else touched the file — a shared folder, a sync client
(OneDrive/Dropbox), or another editor.

**CHECK**
Is the project inside a synced or shared folder?

**FIX**
Move the project to a local, non-synced folder. Each designer keeps their own
local copy (see Getting Started step 9). KiCad Live synchronises live objects
over the network, not files.

**VERIFY**
The dialog stops appearing.

---

## 16. Git or version history fails

**SYMPTOM**
`could not append history for <project>` in the server log.

**CAUSE**
The `data/` directory is not writable, or the disk is full.

**CHECK**
```powershell
Test-Path .\data
New-Item -ItemType File .\data\write-test.tmp; Remove-Item .\data\write-test.tmp
```

**FIX**
Fix the permissions, or point the server elsewhere:
```powershell
$env:KICADLIVE_DATA_DIR = "C:\temp\kicadlive"
python -m server.main
```

History is deliberately **non-fatal**: synchronisation continues even if history
cannot be written, and the failure is logged rather than raised. Git is not
required — the demo must not fail because a Git identity is unconfigured.

**VERIFY**
`data\demo_board.history.jsonl` grows as changes are accepted.

---

## 17. Rotation (or another field) does not synchronise

**SYMPTOM**
Moving a part syncs, but rotating it does not.

**CAUSE**
Historically a bug in this project: a short-circuiting `any()` meant only the
first changed field of an object was applied. Fixed, and covered by tests.

**CHECK**
```powershell
python -m pytest tests\unit -q
```

**FIX**
Make sure you are on current code. The combined move+rotate path is exercised by
`tools/check_env.py` and by the unit tests.

**VERIFY**
Rotate a part with **R** and watch the other machine follow.

---

## 18. A designer's first change after restarting the agent is ignored

**SYMPTOM**
The agent logs `LOCAL moved R1 ...` but nobody receives it.

**CAUSE**
Historically a bug: change ids restarted at 1 each run, so the server's
duplicate-replay guard discarded them. Fixed — ids now carry a per-run token,
and the guard is scoped per client.

**CHECK**
The server logs neither a broadcast nor a conflict for the change.

**FIX**
Update to current code.

**VERIFY**
`tests/unit/test_echo_suppression.py::TestChangeIds` and
`tests/manual/test_live_locks.py` both cover this.

---

## 19. The dashboard is blank or says "Disconnected"

**SYMPTOM**
The page loads but shows no designers.

**CAUSE**
The dashboard follows **one project**, `demo_board` by default. If your agents
use a different project id, the dashboard shows nothing.

**CHECK**
Compare the `Project` line in an agent's output with the dashboard's
**Project** tile.

**FIX**
Open the dashboard with the right project:
`http://SERVER_IP:8000/?project=my_board`

**VERIFY**
The designer list fills in.

---

## 20. Nothing works and the demo is in five minutes

Fall back to the KiCad-free path. It exercises the server, presence, locks,
conflicts and history without any KiCad at all:

```powershell
python tools\test_client.py --server SERVER_IP --name "Designer A"
python tools\test_client.py --server SERVER_IP --name "Designer B"
```

Then follow the backup script in `docs/DEMO.md`. It demonstrates every feature
except live board updates — and it is honest about what is being shown.

---

## 21. "KiCad is busy and cannot respond to API requests now"

**SYMPTOM**
The agent prints `KiCad is busy ... waiting`, or this computer never picks up
work that is already in the project, or its own edits never reach the others.

**CAUSE**
KiCad answers "busy" while a **dialog is open**, while you are **mid-drag or in
an interactive tool**, or while it is **still loading the board**. It is
temporary. Older versions treated it as a lost connection: the initial state
sync gave up and never retried, the user showed as offline, and locks could be
released.

**CHECK**
Look for `KiCad is busy` followed by `KiCad is responding again` in the agent
window. If it never recovers, KiCad still has a dialog open.

**FIX**
1. Update to the current code (`git pull`), then restart the agent.
2. Close any open dialog in KiCad (Preferences, Plot, Board Setup, a message
   box). Modal dialogs block the API entirely.
3. Start the agent only after the board has finished loading.

The agent now waits for KiCad, queues incoming changes **in order**, and applies
them when KiCad responds. Nobody is shown as offline while KiCad is merely busy.
At startup it waits up to 60 s before giving up.

**VERIFY**
`KiCad is responding again` appears, then a change made on another computer
shows up on this board.

**Note on schematics**
Other people's schematic changes cannot appear live in your open eeschema
(KiCad 10 has no API for it). Instead the agent shares the saved `.kicad_sch`
files every 2 minutes and writes teammates' sheets into your project folder;
reload the sheet (File > Revert, or reopen) to see them. See
`docs/SCHEMATIC.md` section 2b.
