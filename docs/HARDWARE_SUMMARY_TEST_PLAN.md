# Hardware Summary — Test Plan

Status recorded: **2026-09-18**. Nothing is marked PASS that was not observed.

```powershell
python -m pytest tests\unit\test_hardware_summary.py -q
```

The governing requirement: **the analyzer must never report hardware the
project does not evidence.** The negative tests below matter more than the
positive ones.

---

## H0 — No external AI or network service

**SETUP** Read the code path end to end.
**ACTION** Trace every call `ProjectAnalyzer.summarise` makes.
**EXPECTED** No HTTP client, no API key, no cloud service.
**ACTUAL** Two sources only:
1. `kicad-cli sch export netlist --format kicadxml` — a local subprocess that
   ships with KiCad, run against a local file.
2. Direct parsing of the local `.kicad_pcb`.

No `requests`, `httpx`, `urllib` or socket use anywhere in
`server/project_analysis/`.
**PASS**

### H0.1 — Verified with all network access blocked

**ACTION** Rather than physically unplugging the machine, `socket.socket` and
`socket.create_connection` are monkeypatched to raise, so *any* attempt to
reach a service fails loudly. Then request the summary.
(`TestNoNetworkAccess::test_summary_works_with_all_sockets_blocked`)

**EXPECTED** Summary generated normally, because nothing in the path needs a
network.

**ACTUAL** Generated with `offline: true`, 6 components, I2C detected — no
socket was ever opened.
**PASS** — this is stronger than pulling the cable: it proves the code path
contains no network call at all, on any machine that runs the suite.

### H0.2 — Guard against a future network dependency

**ACTION** Scan every module in `server/project_analysis/` for `requests`,
`httpx`, `urllib`, `openai`, `anthropic`, `googleapis`.
**ACTUAL** None present.
**PASS** — a regression test, so adding an HTTP call later fails CI.

---

## H1 — Known-project verification

The shipped sample project is the fixture, and its expected content is known by
construction (`tools/make_sample_schematic.py`):

```text
R1  4k7    I2C pull-up
R2  4k7    I2C pull-up
C1  100nF  decoupling
C2  10uF   bulk
U1  BME280 I2C sensor
J1  Conn_01x04 break-out header
Nets: +3V3, GND, SDA, SCL     Board: 2-layer, 90 x 35 mm
```

### H1.1 — All components detected

**EXPECTED** R1, R2, C1, C2, U1, J1.
**ACTUAL** All six, with values and footprints.
**PASS**

### H1.2 — Component inventory is correct

**ACTUAL** Resistors 2, Capacitors 2, Integrated circuits 1, Connectors 1.
**PASS**

### H1.3 — Net count is correct

**EXPECTED** 4 nets.
**ACTUAL** 4 (`/+3V3`, `/GND`, `/SCL`, `/SDA`).
**PASS**

### H1.4 — Board facts are correct

**ACTUAL** 2-layer (F.Cu, B.Cu); 6 footprints; 90.0 × 35.0 mm measured from the
Edge.Cuts outline.
**PASS**

---

## H2 — Interfaces: detected only with evidence

### H2.1 — I2C IS detected (the project has it)

**ACTUAL** `I2C`, confidence `confirmed`, evidence `net /SDA, net /SCL`.
**PASS**

### H2.2 — USB is NOT claimed

The specific false claim called out in the brief.
**EXPECTED** USB absent.
**ACTUAL** Interfaces list is exactly `['I2C']`.
**PASS**

### H2.3 — A capable part alone proves nothing

**ACTION** Analyse a netlist of unnamed GPIO nets.
**EXPECTED** No interfaces.
**ACTUAL** `[]`.
**PASS** — capability is not evidence.

### H2.4 — Half an interface is not enough

**ACTION** Supply `/SDA` with no `/SCL`.
**ACTUAL** `[]`.
**PASS**

### H2.5 — SPI requires 3 of 4 signals

**ACTION** MOSI + MISO only, then MOSI + MISO + SCK.
**ACTUAL** `[]`, then `['SPI']`.
**PASS**

### H2.6 — UART requires both directions

**ACTION** TX only, then TX + RX.
**ACTUAL** `[]`, then `['UART']`.
**PASS**

### H2.7 — USB IS detected when the evidence exists

**ACTION** Nets `/USB_D+` and `/USB_D-`.
**ACTUAL** `['USB']`.
**PASS** — the rule is evidence-based, not a blanket refusal.

> Regression fixed during testing: the original USB patterns used `\bD\+\b`,
> which can never match — `+` is not a word character, and `_` is, so
> `USB_D+` failed both boundaries. Now anchored on separators. Covered by H2.7.

### H2.8 — Evidence says WHY it matched

**ACTION** Detect I2C from pin functions rather than net names.
**EXPECTED** The evidence names the pin, not just the net.
**ACTUAL** `U1.5 pin function SDA_5 on net /DATA-RB7`.
**PASS** — a reader can verify the claim against the schematic.

---

## H3 — Components: no invented parts

### H3.1 — No microcontroller claimed when there is none

The sample board is a sensor plus a header; there is no MCU.
**EXPECTED** `Not available`, with a reason.
**ACTUAL** `microcontrollers: []`; headline shows `Not available`, evidence
`no recognised MCU part number in the schematic`.
**PASS**

### H3.2 — A real MCU IS recognised

**ACTION** Analyse a component valued `ESP32-WROOM-32`.
**ACTUAL** family `ESP32`.
**PASS**

### H3.3 — Designator classification

**ACTUAL** R→Resistors, C→Capacitors, U→Integrated circuits, J→Connectors,
LED1→LEDs (not Inductors), L1→Inductors.
**PASS**

### H3.4 — Missing footprints are reported, not hidden

**ACTUAL** Components without a footprint listed under
`unpopulated_footprints`.
**PASS**

---

## H4 — Unknowns are explicit

### H4.1 — Every unknown says why

**ACTION** Inspect each headline line.
**EXPECTED** A `None` value always carries an evidence string.
**ACTUAL** Verified for every line; e.g. board size falls back to
`no Edge.Cuts outline; components span W x H mm` when no outline exists.
**PASS**

### H4.2 — A missing schematic degrades gracefully

**ACTION** Point the analyzer at a directory with no `.kicad_sch`.
**EXPECTED** A warning, not a crash; board analysis still runs.
**ACTUAL** `warnings: ["No .kicad_sch found; schematic analysis unavailable."]`.
**PASS**

### H4.3 — A missing `kicad-cli` degrades gracefully

**ACTUAL** `NetlistUnavailable` caught; recorded as a warning; the board half of
the summary still produced.
**PASS**

### H4.4 — Malformed netlist XML is rejected

**ACTUAL** `NetlistUnavailable`, not a crash or a partial result.
**PASS**

---

## H5 — Performance

| Project | Components | Nets | Time |
|---|---|---|---|
| `sample_project` | 6 | 4 | **294 ms** |
| KiCad `pic_programmer` | 63 | 111 | **388–420 ms** |

Cached for 30 s, so repeated dashboard loads are free.
**PASS**

---

## Offline run — repeating it yourself

The automated proof (H0.1) is the authoritative one. To satisfy yourself by
hand:

1. Disable the network adapter, or pull the Ethernet cable / turn off Wi-Fi.
2. Start the server: `python -m server.main`
3. Open `http://localhost:8000/project.html`

**Expected:** the summary renders normally and the page states
*"Generated locally in N ms - no internet connection used"*.

No request is made because none exists in the code path.

---

## Results

| ID | Test | Result |
|---|---|---|
| H0, H0.1, H0.2 | No external AI/network; proven with sockets blocked | PASS |
| H1.1–H1.4 | Known project: components, inventory, nets, board | PASS |
| H2.1 | I2C detected (present) | PASS |
| H2.2 | **USB not claimed (absent)** | PASS |
| H2.3–H2.6 | Insufficient evidence yields nothing | PASS |
| H2.7 | USB detected when evidenced | PASS |
| H2.8 | Evidence explains the match | PASS |
| H3.1 | **No MCU claimed when there is none** | PASS |
| H3.2–H3.4 | Real MCU, designators, missing footprints | PASS |
| H4.1–H4.4 | Unknowns explicit, graceful degradation | PASS |
| H5 | Performance | PASS |

## Limitations, stated plainly

* Interface detection relies on **conventional signal naming**. A board whose
  I2C nets are called `NET1`/`NET2` will not be detected — correctly, since
  there is no evidence.
* Component families are recognised from a **fixed pattern list**; an
  unusual part will land in the inventory but not be named as an MCU or sensor.
* Board size needs an **Edge.Cuts outline**; without one the component extent is
  reported instead, and clearly labelled as such.
* The netlist is read **from the saved file**, so unsaved schematic edits are
  not reflected until Ctrl+S.
