# KiCad Live — Getting Started

This is an operational guide. Follow it top to bottom and you will have five
computers collaborating on one PCB. It assumes no prior knowledge of the
project.

Every command here has been run on the development machine. Where output is
shown, that is the output actually produced.

---

## 1. Requirements

| Thing | Requirement | Tested with |
|---|---|---|
| Operating system | Windows 10/11 | Windows 11 Pro (10.0.26200) |
| KiCad | **9.0 or newer** (needs the IPC API) | **10.0.6** |
| Python | 3.11 or newer | 3.14.7 |
| Git | any recent version (only to clone) | 2.55.0 |
| Network | all machines on one LAN, TCP port 8000 reachable | — |
| Browser | any modern browser, for the dashboard | — |

> **KiCad 8 and earlier will not work.** KiCad Live drives a *running* KiCad
> through the IPC API, which was introduced in KiCad 9. Check your version with
> **Help → About KiCad**.

Python packages (installed in step 3, pinned in `requirements.txt`):
`fastapi`, `uvicorn`, `websockets`, `kicad-python`, plus `pytest`/`httpx` for tests.

Linux and macOS: the server, agent, dashboard and tests are plain Python and
should work, but **only Windows has been tested**. The firewall and plugin-path
instructions below are Windows-specific.

---

## 2. Clone the repository

Do this on **every** computer — server and clients alike.

```powershell
git clone <YOUR_REPO_URL> kicad-live
cd kicad-live
```

If you do not have a Git remote, copy the whole `kicad-live` folder to each
machine with a USB stick or a network share. Either way, **every machine must
end up with the same copy of `sample_project/demo_board.kicad_pcb`** — see
step 9 for why that matters.

---

## 3. Install the Python dependencies

On every computer:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell refuses to run the activation script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You should see `(.venv)` at the start of your prompt. **Every `python` command
below assumes the virtual environment is active.**

---

## 4. Decide which computer does what

**Use Configuration A** — it is the recommended layout.

```text
Computer 1 = SERVER   (may also run KiCad as a designer)
Computer 2 = DESIGNER A
Computer 3 = DESIGNER B
Computer 4 = DESIGNER C
Computer 5 = DESIGNER D
```

The server is an in-memory Python process that idles at a few megabytes and
near-zero CPU with five clients, so dedicating a whole machine to it wastes a
seat. Running the dashboard on the same machine also lets the presenter drive
the demo and show the dashboard together.

Configuration B (Computer 1 does nothing but serve) also works and needs no
code changes — just do not start an agent on Computer 1.

### Find the server's LAN IP

On **Computer 1**:

```powershell
ipconfig | Select-String "IPv4"
```

Output looks like:

```text
   IPv4 Address. . . . . . . . . . . : 192.168.1.50
```

Write that address down. Everywhere below, **`SERVER_IP` means this address.**
Pick the one on the same subnet as the other machines; ignore addresses that
start with `169.254.` (no DHCP) or belong to VirtualBox/WSL adapters.

---

## 5. Open the firewall (Computer 1 only)

**This step is mandatory.** Windows blocks incoming connections by default, and
skipping it is the single most common reason clients cannot connect.

Run PowerShell **as Administrator** on Computer 1:

```powershell
New-NetFirewallRule -DisplayName "KiCad Live" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

To confirm it exists:

```powershell
Get-NetFirewallRule -DisplayName "KiCad Live"
```

To remove it after the demo:

```powershell
Remove-NetFirewallRule -DisplayName "KiCad Live"
```

Linux (if you use the server on Linux):

```bash
sudo ufw allow 8000/tcp
```

---

## 6. Start the server (Computer 1)

```powershell
python -m server.main
```

Expected output:

```text
==============================================================
 KiCad Live Sync Server 1.0.0
 Listening on   0.0.0.0:8000
 WebSocket      ws://0.0.0.0:8000/ws
 Health         http://0.0.0.0:8000/health
 Dashboard      http://0.0.0.0:8000/
 History data   D:\vinhack\kicad-live\data
==============================================================
```

Leave this window open. `0.0.0.0` means "listen on every network interface",
which is what lets the other computers reach it.

To use a different port: `python -m server.main --port 9000` (and open that port
in the firewall instead).

---

## 7. Verify the server

On **Computer 1**, in a browser: <http://127.0.0.1:8000/health>

On **any other computer**, in a browser: `http://SERVER_IP:8000/health`

Expected response:

```json
{"status": "ok", "server_version": "1.0.0", "uptime_seconds": 12.4, "clients": 0, "projects": []}
```

The dashboard is at `http://SERVER_IP:8000/` — open it now and leave it on
screen. It will fill in as designers connect.

If the health check works locally but not from another computer, the firewall
rule in step 5 is missing or the IP is wrong. From a client machine:

```powershell
Test-NetConnection -ComputerName SERVER_IP -Port 8000
```

`TcpTestSucceeded : True` means the network path is fine.

---

## 8. Enable the KiCad API (every computer running KiCad)

KiCad Live talks to a **running** KiCad. That interface is off by default.

**Close KiCad completely first** — KiCad rewrites its settings when it exits and
would undo the change.

Either use the GUI:

> **Preferences → Plugins → tick "Enable KiCad API" → OK → restart KiCad**

Or run:

```powershell
python tools\enable_kicad_api.py
```

Expected:

```text
[ OK ] KiCad 10.0: API server ENABLED  (backup: kicad_common.json.kicadlive-backup)

Now start KiCad and open a PCB. The agent can then connect.
```

Check it at any time without changing anything:

```powershell
python tools\enable_kicad_api.py --check
```

---

## 9. Prepare the demo project (every computer running KiCad)

**Each designer opens their own local copy of the project. This is the one
supported workflow.**

Why this and not a shared network folder: KiCad takes an exclusive lock on an
open board and warns loudly when a file changes underneath it. Pointing five
KiCad instances at one file on a share produces lock errors and
"file has changed on disk" dialogs all demo long. KiCad Live does not need a
shared file at all — it synchronises **live board objects over the network**,
so local copies are both simpler and safer.

The one thing that *must* be true: **every copy must descend from the same
file**, because synchronisation is keyed on KiCad's footprint UUIDs. Cloning
the repository gives everyone byte-identical copies, so this is automatic.

Open the board on each designer machine:

```powershell
& "C:\Program Files\KiCad\10.0\bin\pcbnew.exe" sample_project\demo_board.kicad_pcb
```

Or start KiCad and open `sample_project/demo_board.kicad_pro`, then open the PCB
editor.

The demo board holds six parts: **R1, R2, C1, C2, U1, J1**.

> Want to use your own board instead? That works — just make sure every machine
> starts from the *same* copy of it. A board that was independently re-created
> on each machine will have different UUIDs and will not synchronise.

### Check the machine is ready

With KiCad open and the board loaded:

```powershell
python tools\check_env.py --server SERVER_IP
```

Expected:

```text
[PASS] Python 3.11 or newer               - 3.14.7
[PASS] kipy importable                    - kicad-python 0.8.0
[PASS] websockets importable
[PASS] KiCad API enabled in settings      - 10.0
[PASS] Connected to KiCad                 - 10.0.6 (10.0.6)
[PASS] Board open                         - demo_board.kicad_pcb
[PASS] Footprints readable                - 6 footprints
       C1, C2, J1, R1, R2, U1
[PASS] Board poll latency                 - 2.1 ms average, 2.9 ms worst
[PASS] Board is writable                  - moved and restored C1
[PASS] Sync server reachable              - http://SERVER_IP:8000/health

RESULT: all 10 checks PASSED - this machine is ready.
```

**Do not continue past a failing check.** Each failure names the fix, and
`docs/TROUBLESHOOTING.md` has more detail.

---

## 10. Install the KiCad plugin (optional but recommended)

The plugin adds a button inside KiCad that starts the agent for you. The agent
can also be started from a terminal, so this step is a convenience.

With **KiCad closed**:

```powershell
python tools\install_plugin.py
```

Expected:

```text
[ OK ] installed to C:\Users\<you>\Documents\KiCad\10.0\scripting\plugins\kicad_live
       repo_root  = D:\...\kicad-live
       python_exe = D:\...\kicad-live\.venv\Scripts\python.exe

Installed into 1 location(s).
Restart KiCad, then look for:
  Tools > External Plugins > "KiCad Live - Start collaboration"
```

Start KiCad, open the PCB editor, and check the menu:

> **Tools → External Plugins → KiCad Live - Start collaboration**

To verify the install without opening the menu:

```powershell
& "C:\Program Files\KiCad\10.0\bin\python.exe" tests\manual\test_plugin_loads.py
```

To remove or reinstall:

```powershell
python tools\install_plugin.py --uninstall
python tools\install_plugin.py
```

KiCad only scans for plugins at startup, so **restart KiCad after installing**.
If the menu entry is missing, read
`Documents\KiCad\10.0\scripting\plugins\kicad_live\last_load.json` — it records
whether the plugin loaded and, if not, the exact error.

---

## 11. FIRST RUN — the two-computer test

Do this before attempting five machines. It takes about five minutes.

```text
STEP 1  On Computer 1, start the server:
            python -m server.main

STEP 2  On Computer 1, open the dashboard:
            http://SERVER_IP:8000/

STEP 3  On Computer 2, open KiCad with sample_project\demo_board.kicad_pcb

STEP 4  On Computer 2, start the agent:
            python -m agent.main --server SERVER_IP --name "Designer A"
        (or Tools > External Plugins > KiCad Live)

STEP 5  Confirm the agent prints:
            Board      demo_board.kicad_pcb
            Project    demo_board
            connected to server as 'Designer A'

STEP 6  On Computer 3, open the SAME board from its own clone.

STEP 7  On Computer 3, start the agent:
            python -m agent.main --server SERVER_IP --name "Designer B"

STEP 8  The dashboard now lists Designer A and Designer B.

STEP 9  On Computer 2, drag R1 somewhere obvious.

STEP 10 Watch Computer 3.

EXPECTED RESULT:
    R1 moves on Computer 3 by itself.
```

### How fast is it, really?

Measured on the development machine by `tests/manual/test_live_sync.py`:

| Path | Measured |
|---|---|
| A remote change appearing in live KiCad | **0.22 s** |
| An edit in KiCad reaching the other client | **0.03 s** |

The agent polls the board every **250 ms**, so end-to-end latency is that poll
interval plus network time. On a normal LAN expect **well under one second**,
typically around a quarter of a second. It is not instantaneous, and it does not
need you to press Ctrl+S.

### Things worth trying immediately

* **Presence.** Click R1 on Computer 2. The dashboard shows
  `Designer A — Editing R1`, and R1 appears in the lock list. Selecting a part
  locks it automatically; deselecting releases it.
* **Locking.** With R1 selected on Computer 2, try to move R1 on Computer 3.
  Computer 3's agent prints `R1 is currently locked by Designer A` and puts R1
  straight back.
* **Undo.** Press Ctrl+Z on the machine that *received* a change. Remote changes
  land on KiCad's normal undo stack.

---

## 12. Five-computer setup

Once two machines work, add the rest. Each designer machine repeats steps 2, 3,
8, 9 and then starts an agent with a distinct `--name`.

```text
SERVER      Computer 1   python -m server.main
DESIGNER A  Computer 2   python -m agent.main --server SERVER_IP --name "Designer A"
DESIGNER B  Computer 3   python -m agent.main --server SERVER_IP --name "Designer B"
DESIGNER C  Computer 4   python -m agent.main --server SERVER_IP --name "Designer C"
DESIGNER D  Computer 5   python -m agent.main --server SERVER_IP --name "Designer D"
```

### Pre-flight checklist

```text
[ ] Computer 1 connected to LAN
[ ] Server started
[ ] Firewall configured (port 8000 inbound allowed)
[ ] Health endpoint works from ANOTHER computer
[ ] Dashboard open on Computer 1
[ ] KiCad API enabled on every designer machine
[ ] Same demo_board.kicad_pcb on every designer machine
[ ] check_env.py passes on every designer machine
[ ] Computer 2 connected
[ ] Computer 3 connected
[ ] Computer 4 connected
[ ] Computer 5 connected
[ ] All four designers visible on the dashboard
[ ] Demo project loaded everywhere
[ ] Synchronisation tested   (move R1, watch the others)
[ ] Locking tested           (two people select the same part)
[ ] Conflict detection tested
```

The full procedure with expected results for each test is in
`docs/FIVE_COMPUTER_TEST.md`.

---

## 13. Agent options

```text
--server SERVER_IP     required; the sync server's address
--name "Designer A"    required; your display name
--port 8000            server port (default 8000)
--project demo_board   project id (default: the open board's filename)
--read-only            receive changes but never send any
--poll-interval 0.25   board poll period in seconds
--verbose              debug logging
```

Everyone editing the same board must share a `project_id`. By default it is
derived from the board's filename, so opening `demo_board.kicad_pcb` everywhere
puts everyone in the `demo_board` project automatically.

### Stopping

Press **Ctrl+C** in the agent window. It prints a session summary:

```text
 Session totals: sent=14 received=9 conflicts=1 blocked-by-lock=2
```

Stopping the agent has no effect on KiCad or your board. Nothing is lost —
KiCad Live never saves your file; use Ctrl+S as usual when you are done.

---

## 14. What KiCad Live does and does not do

**Does:**
* Synchronises footprint position, rotation, layer, value and reference, live.
* Shows who is connected and what they have selected.
* Soft-locks components as people select them.
* Detects conflicting edits and refuses to guess between them.
* Records a version history of every accepted change.

**Does not:**
* Synchronise tracks, vias, zones or schematics (see the stretch goals).
* Save your board for you.
* Enforce locks inside KiCad itself — locks are advisory and honoured by the
  agent.
* Provide any authentication or encryption. **This is a LAN prototype.**

---

## 15. If something goes wrong

`docs/TROUBLESHOOTING.md` covers every failure encountered while building this,
each with SYMPTOM / CAUSE / CHECK / FIX / VERIFY.

The three fastest checks:

```powershell
python tools\check_env.py --server SERVER_IP     # this machine
curl http://SERVER_IP:8000/health                # the server
python tools\enable_kicad_api.py --check         # the KiCad setting
```
