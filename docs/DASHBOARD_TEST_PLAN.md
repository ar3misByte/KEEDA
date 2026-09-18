# Dashboard — Test Plan

Status recorded: **2026-09-18**.

**An important honesty note.** The dashboard was developed without a browser
available to the developer. Everything the server sends and every asset it
serves has been verified automatically. The **rendering and interaction** have
not been observed by a human and are marked **NOT RUN**, with the exact steps
to run them. Nothing visual is claimed as PASS on the basis of "the code looks
right".

Automated portion:

```powershell
python -m pytest tests\integration\test_collab_integration.py -q -k "rest or dashboard"
```

---

# Part 1 — Verified automatically (PASS)

## D1 — Pages and assets are served

**ACTION** Request every page and asset.
**EXPECTED** HTTP 200 for each.
**ACTUAL**

```text
/                200      /activity.html    200
/index.html      200      /comments.html    200
/schematic.html  200      /project.html     200
/pcb.html        200      /static/app.js    200
                          /static/style.css 200
```
**PASS**

## D2 — Unknown and hostile paths are refused

**ACTION** Request a page name containing separators or dots.
**EXPECTED** 404, no filesystem escape.
**ACTUAL** 404. The handler accepts only alphanumeric page names, so `../`
cannot be expressed in the route at all.
**PASS**

## D3 — REST endpoints respond

**ACTUAL** All 200: `/api/overview`, `/api/activity`, `/api/comments`,
`/api/users`, `/api/versions`, `/api/schematic`, `/api/pcb`, `/api/summary`,
`/api/whats-new`, `/api/stats`.
**PASS**

## D4 — Invalid project ids are rejected

**ACTION** `GET /api/overview?project=../etc/passwd`
**ACTUAL** HTTP 400.
**PASS**

## D5 — `/api/overview` carries everything the Overview page needs

**ACTION** Inspect the payload against the fields `app.js` reads.
**EXPECTED** `clients_online`, `clients`, `locks`, `lock_count`,
`changes_today`, `open_comments`, `open_conflicts`, `version`,
`recent_activity`.
**ACTUAL** All present. Live sample:
`{'clients_online': 0, 'changes_today': 31, 'open_comments': 0,
'open_conflicts': 0, 'version': 17}`.
**PASS**

## D6 — Activity events carry every change-inspector field

**ACTION** Inspect an event against the fields the inspector renders.
**EXPECTED** `event_id`, `ts`, `username`, `domain`, `object_type`,
`object_id`, `object_ref`, `action`, `field`, `old_value`, `new_value`,
`description`, `version`.
**ACTUAL** All present; structured values round-trip as objects, so a position
arrives as `{"x": …, "y": …}` and can be rendered as `X = … Y = …`.
**PASS**

## D7 — Live dashboard messages are emitted

**ACTION** Trigger each event type with a scripted client and observe the
WebSocket.
**ACTUAL** `presence_update`, `lock_update`, `activity`, `comment_created`,
`comment_updated`, `conflict_event`, `blocked_event`, `whats_new` and
`comments` are all sent, with the payload shapes `app.js` consumes.
**PASS**

## D8 — No external dependencies

**ACTION** Inspect the served HTML.
**EXPECTED** No CDN, framework download or external font.
**ACTUAL** Two local assets only: `/static/style.css`, `/static/app.js`. The
dashboard needs no internet connection.
**PASS** — deliberate: five demo machines should need nothing installed.

---

# Part 2 — Requires a browser (NOT RUN)

Run these with the server up and at least two designers connected. Tick each
one; do not mark PASS until observed.

## D9 — Overview renders

```text
[ ] Six stat tiles populate (designers, changes today, locks, comments,
    conflicts, version)
[ ] Active users list shows each designer with their status text
[ ] Lock panel shows "SCH R1 -> Aditya  118s left" with a ticking countdown
[ ] Recent activity lists the newest entries
[ ] Connecting a second designer updates the page with no reload
```

## D10 — Activity timeline

```text
[ ] Events listed newest first, grouped under day headers
[ ] User / domain / action filters narrow the list and the count
[ ] The user dropdown lists only users who actually appear
[ ] Conflicts show a red left border, lock denials amber, comments blue
```

## D11 — Change inspector

```text
[ ] Clicking an activity row opens the modal
[ ] User, time, domain, object, action, property are shown
[ ] A move shows "X = 42.5   Y = 31.2", not raw JSON
[ ] Absent fields read "Not available"
[ ] Close and click-outside both dismiss it
```

## D12 — Comments

```text
[ ] Typing in the object field autocompletes real references (R1, U1, ...)
[ ] Posting creates a thread visible to the other designer immediately
[ ] An unknown reference is refused with a toast, not silently accepted
[ ] Reply posts on Enter and appears indented
[ ] Resolve / Reopen toggles the badge and the sidebar count
[ ] All / Open / Resolved / Mine each filter correctly
```

## D13 — Schematic and PCB pages

```text
[ ] Schematic lists symbols with value, sheet and footprint
[ ] PCB lists footprints with position and layer
[ ] Locked rows show "locked by <name>"
[ ] Commented rows show an open count
[ ] Clicking a row opens a comment box anchored to that object
[ ] The schematic page shows the "detected and reviewed, never applied" note
```

## D14 — Hardware summary page

```text
[ ] Headline lines render with their evidence underneath
[ ] Unknowns render as italic grey "Not available"
[ ] Interfaces, power nets, board facts and inventory all populate
[ ] Footer reads "Generated locally in N ms - no internet connection used"
```

## D15 — Live notifications

```text
[ ] A comment raises a blue toast
[ ] A conflict raises a red toast naming both users
[ ] A lock denial raises an amber toast
[ ] Toasts dismiss themselves after a few seconds
```

## D16 — Reconnection

```text
[ ] Stopping the server turns the status pill red ("Disconnected")
[ ] Restarting it reconnects without a page reload
[ ] The page repopulates with current state
```

---

## Results

| ID | Test | Result |
|---|---|---|
| D1–D2 | Pages served; hostile paths refused | PASS |
| D3–D4 | REST endpoints; project id validation | PASS |
| D5 | Overview payload complete | PASS |
| D6 | Change-inspector fields complete | PASS |
| D7 | Live message types emitted | PASS |
| D8 | No external dependencies | PASS |
| D9–D16 | Rendering and interaction | **NOT RUN** (needs a browser) |

## Out of scope

Graphical rendering of the board or schematic. Rebuilding pcbnew or eeschema in
a browser is explicitly not what this tool is for; the dashboard shows
structured views, locks, comments and activity instead.
