# KiCad Live — Demo Script

Target length: **3–5 minutes**. Seven scenes.

## Before you walk on stage

```text
[ ] Server running on Computer 1
[ ] Dashboard open full-screen on the projector
[ ] KiCad open on Computers 2–5, demo_board.kicad_pcb loaded
[ ] Agents running on at least Computers 2 and 3
[ ] check_env.py passed on every machine
[ ] R1 and C1 visible without scrolling in every KiCad window
[ ] Zoom level matched across machines so movement is obvious
[ ] Agent console windows visible but not covering the board
[ ] Screen sleep disabled:  powercfg /change standby-timeout-ac 0
```

Rehearse once end to end. The five-computer procedure in
`docs/FIVE_COMPUTER_TEST.md` is that rehearsal.

---

## Scene 1 — The problem (30 s)

**Show:** two KiCad windows side by side, same board.

**Say:**

> "Two engineers, one board. Today the only way to share this is to send a file
> and hope nobody else touched it. There's no way to see what anyone else is
> doing, and merging two people's changes to a PCB by hand is miserable.
>
> We didn't fork KiCad. This is stock KiCad 10, unmodified."

---

## Scene 2 — Presence (30 s)

**Do:** switch to the dashboard.

**Show:** designers listed, online, with what they have selected.

**Say:**

> "This is the KiCad Live dashboard. Everyone who's connected shows up here —
> and it's not something they have to announce. When Designer A clicks a
> component, we can see it."

**Do:** click **U1** on Computer 2.

**Show:** dashboard updates to `Designer A — Editing U1`.

---

## Scene 3 — Live synchronisation (60 s) — *the money shot*

**Say:**

> "Watch the right-hand screen."

**Do:** on Computer 2, drag **R1** somewhere obvious.

**Show:** R1 moves on Computer 3 by itself, in about a quarter of a second.

**Say:**

> "No save. No file transfer. No reload dialog. I didn't press Ctrl+S — that
> change went straight into the other running KiCad's live board.
>
> And it's a proper KiCad edit — watch."

**Do:** on **Computer 3** press **Ctrl+Z**.

**Show:** R1 goes back.

**Say:**

> "Remote changes land on KiCad's normal undo stack. Nothing about the way you
> use KiCad changes."

> **If asked how:** KiCad 9 added an IPC API. A separate agent process reads and
> writes the live board — so if our code crashes, it cannot take KiCad or your
> unsaved work with it.

---

## Scene 4 — Soft locking (45 s)

**Say:**

> "Two people editing the same part is where this normally falls apart."

**Do:** on Computer 2, select **R1** (just click it).

**Show:** dashboard Locks panel: `R1 → Designer A`.

**Say:**

> "Selecting a component claims it. Nobody presses a 'lock' button — you just
> click the part you're about to move."

**Do:** on Computer 3, try to move **R1**.

**Show:** Computer 3's console:

```text
  >>> R1 is currently locked by Designer A - your change was reverted.
```

R1 snaps back on Computer 3. The dashboard logs the blocked attempt.

**Say:**

> "Refused, and put back, so the two boards can't drift apart. These are *soft*
> locks — enforced by the agent, not by KiCad. We say that plainly in the UI,
> because a lock that claims to be stronger than it is would be worse than none."

**Do:** press **Esc** on Computer 2, then move R1 on Computer 3.

**Show:** it works now.

---

## Scene 5 — Multiple designers (30 s)

**Do:** bring Computers 4 and 5 into shot.

**Show:** dashboard says **4 designers online**.

**Say:**

> "Four designers, one board."

**Do:** count down, then all four move a different component at once
(R1, C1, U1, J1).

**Show:** every board ends up with all four changes. Activity feed lists four
entries with rising version numbers.

**Say:**

> "Different components never conflict — those merge cleanly."

---

## Scene 6 — Conflict detection (45 s)

**Say:**

> "So what happens when they *do* collide?"

**Do:** have two designers change the same component without either selecting it
first, within the same poll window.

**Show:** the losing machine's console:

```text
==============================================================
  CONFLICT on R1.position
    you changed it to : {'x': 40.0, 'y': 35.0}
    Designer A changed it to : {'x': 45.0, 'y': 30.0}
    your base v7, server is at v8
  KiCad Live will not guess. Reverting to the server's value.
  To keep YOUR value instead, move R1 again now.
==============================================================
```

**Say:**

> "It tells you the component, the field, both values, and who you collided
> with. And then it stops.
>
> We deliberately don't auto-merge two people's intentions about where a part
> belongs. There's no correct answer a computer can pick. Being honest that a
> human has to decide is worth more than looking clever."

---

## Scene 7 — History (30 s)

**Do:** scroll the dashboard's Activity feed.

**Show:**

```text
17:42:31  Designer A  moved R1 to (65.00, 50.00)   v15
17:42:44  Designer B  moved C2 to (82.50, 60.00)   v16
17:42:58  Designer C  rotated U1 to 90 deg          v17
```

**Say:**

> "Every accepted change is versioned, attributed and timestamped, and written
> to disk — so it survives a server restart.
>
> To recap: presence, live sync, soft locking, structured conflict detection and
> version history — on unmodified KiCad, over a plain LAN, no cloud."

---

## Questions you should expect

**"Is this really Google Docs for KiCad?"**
> No, and we don't claim it. There's no character-level merging. It's structured
> object synchronisation with locking and conflict detection. That's the part
> that's genuinely useful and that we can make reliable.

**"What syncs?"**
> Footprints: position, rotation, layer, value, reference. Tracks, vias, zones
> and schematics are the next step, not in this build.

**"What if the server dies?"**
> Every KiCad keeps working — nothing is blocked. Agents retry and resync
> automatically. History is on disk; object state is re-seeded from a live board
> by the first agent back.

**"What about security?"**
> It's a LAN prototype. No auth, no encryption. We validate every message and
> confine file access, but we'd not put this on an untrusted network, and we say
> so in the docs.

**"How much latency?"**
> Measured: 0.22 s for a remote change to appear in live KiCad; the server's own
> fan-out is under 1.5 ms at the 95th percentile with five clients. The limit is
> our 250 ms poll interval, not the network.

**"Did you modify KiCad?"**
> No. Stock KiCad 10.0.6, one preference ticked, and an optional plugin that
> only launches our agent.

---

# FAILURE-SAFE DEMO

If live synchronisation misbehaves, **do not fake it.** Say what broke and show
what does work — the backup path exercises everything except live board updates.

## Backup procedure

On Computer 1, start two KiCad-free clients:

```powershell
python tools\test_client.py --server SERVER_IP --name "Designer A"
python tools\test_client.py --server SERVER_IP --name "Designer B"
```

Then:

1. **Dashboard** — server connected, project, client count.
2. **Presence** — both designers listed.
3. **Recorded changes** — Activity feed with versions, names and timestamps.
4. **Locking** — one client locks R1, the other is denied; show the denial.
5. **Conflict detection** — force a conflict; show the structured report.
6. **Explain the failure** honestly:
   > "Live board sync isn't coming through on this machine right now — most
   > likely the KiCad API setting or the network. What you're seeing is the same
   > server and the same protocol, driven by test clients instead of KiCad. The
   > board-level part is verified by `tests/manual/test_live_sync.py`, which I
   > can run for you."

If **one** designer machine is broken but another works, drop to a two-computer
demo and say so. Two computers working beats five computers half-working.

## Fast fixes during a demo

| Symptom | Fix |
|---|---|
| Agent can't reach KiCad | Close KiCad, `python tools\enable_kicad_api.py`, reopen |
| Agent can't reach server | `Test-NetConnection SERVER_IP -Port 8000`; check the firewall rule |
| Changes not syncing | Check both agents print the same `Project` |
| One board out of step | Restart that agent — it re-adopts server state on connect |
| Lock stuck | Wait 120 s, or the owner deselects |
| `commit in progress` error | Restart KiCad on that machine |

Full detail: `docs/TROUBLESHOOTING.md`.
