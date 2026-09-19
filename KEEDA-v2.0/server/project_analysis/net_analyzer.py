"""Identify interfaces and power rails from netlist evidence.

The governing rule: every claim carries the evidence that produced it. If the
evidence is not there, the answer is "Not available" - never a guess.

A component being *capable* of I2C is not evidence that the board uses I2C.
Only named nets and pin functions count, because those are things a human
actually drew.
"""
from __future__ import annotations

import re

# Each rule: required signal groups, and how many distinct groups must match.
# Patterns are matched against the last path segment of a net name and against
# pin functions, both upper-cased.
INTERFACE_RULES = [
    {
        "name": "I2C",
        "groups": [[r"\bSDA\b", r"\bI2C[_-]?SDA\b"],
                   [r"\bSCL\b", r"\bI2C[_-]?SCL\b", r"\bSCK?L\b"]],
        "required": 2,
    },
    {
        "name": "SPI",
        "groups": [[r"\bMOSI\b", r"\bSDI\b", r"\bCOPI\b"],
                   [r"\bMISO\b", r"\bSDO\b", r"\bCIPO\b"],
                   [r"\bSCK\b", r"\bSCLK\b", r"\bSPI[_-]?CLK\b"],
                   [r"\bCS\b", r"\bNSS\b", r"\bSS\b", r"\bCS[0-9]?\b"]],
        "required": 3,
    },
    {
        "name": "UART",
        "groups": [[r"\bTX\b", r"\bTXD\b", r"\bUART[_-]?TX\b"],
                   [r"\bRX\b", r"\bRXD\b", r"\bUART[_-]?RX\b"]],
        "required": 2,
    },
    {
        # NOTE: '+' and '-' are not word characters, so a trailing \b after
        # them can never match; and '_' IS a word character, so a leading \b
        # fails on names like USB_D+. Anchor on a separator or string start
        # instead.
        "name": "USB",
        "groups": [[r"(?:^|[_\-])D\+", r"(?:^|[_\-])DP(?:$|[_\-])", r"^DP$"],
                   [r"(?:^|[_\-])D-", r"(?:^|[_\-])DM(?:$|[_\-])", r"^DM$"]],
        "required": 2,
    },
    {
        "name": "CAN",
        "groups": [[r"\bCANH\b", r"\bCAN[_-]?H\b"],
                   [r"\bCANL\b", r"\bCAN[_-]?L\b"]],
        "required": 2,
    },
    {
        "name": "JTAG",
        "groups": [[r"\bTCK\b"], [r"\bTMS\b"], [r"\bTDI\b"], [r"\bTDO\b"]],
        "required": 3,
    },
    {
        "name": "SWD",
        "groups": [[r"\bSWDIO\b"], [r"\bSWCLK\b"]],
        "required": 2,
    },
    {
        "name": "I2S",
        "groups": [[r"\bLRCK\b", r"\bWS\b", r"\bI2S[_-]?WS\b"],
                   [r"\bBCLK\b", r"\bI2S[_-]?SCK\b"],
                   [r"\bSDIN\b", r"\bSDOUT\b", r"\bI2S[_-]?SD\b"]],
        "required": 2,
    },
]

POWER_PATTERNS = [
    (r"^\+?(\d+)V(\d+)$", None),      # +3V3, 5V0
    (r"^\+?(\d+)V$", None),           # +5V, 12V
    (r"^VCC$", None), (r"^VDD$", None), (r"^VBUS$", None),
    (r"^VIN$", None), (r"^VBAT$", None), (r"^AVDD$", None), (r"^VDDA$", None),
]

GROUND_PATTERNS = [r"^GND$", r"^AGND$", r"^DGND$", r"^GNDA$", r"^VSS$", r"^0V$"]


def _leaf(net_name: str) -> str:
    """'/pic_sockets/VCC_PIC' -> 'VCC_PIC'."""
    return (net_name or "").rstrip("/").split("/")[-1].upper()


def _signal_tokens(net: dict) -> list[tuple[str, str]]:
    """Every token about a net that counts as human-authored evidence.

    Returns (token, human-readable source) pairs. The source matters: an
    interface matched on a pin function must SAY it matched on a pin function,
    or the summary looks like it invented the claim.
    """
    tokens: list[tuple[str, str]] = []
    leaf = _leaf(net.get("name", ""))
    if leaf:
        tokens.append((leaf, f"net {net.get('name')}"))
    for node in net.get("nodes", []):
        function = (node.get("function") or "").upper()
        if not function:
            continue
        where = (f"{node.get('reference')}.{node.get('pin')} pin function "
                 f"{function} on net {net.get('name')}")
        # KiCad appends the pin number, e.g. 'SCL_6'. Keep both forms.
        tokens.append((function, where))
        stripped = re.sub(r"_\d+$", "", function)
        if stripped != function:
            tokens.append((stripped, where))
    return tokens


def detect_interfaces(nets: list[dict]) -> list[dict]:
    """Interfaces, each with the specific evidence that proves it."""
    token_map: list[tuple[str, str]] = []
    for net in nets:
        token_map.extend(_signal_tokens(net))

    results = []
    for rule in INTERFACE_RULES:
        matched_groups = 0
        evidence: list[str] = []
        for group in rule["groups"]:
            hit = None
            for pattern in group:
                for token, source in token_map:
                    if re.search(pattern, token):
                        hit = source
                        break
                if hit:
                    break
            if hit:
                matched_groups += 1
                if hit not in evidence:
                    evidence.append(hit)
        if matched_groups >= rule["required"]:
            results.append({
                "name": rule["name"],
                "confidence": "confirmed" if matched_groups == len(rule["groups"]) else "likely",
                "evidence": evidence,
                "matched": matched_groups,
                "of": len(rule["groups"]),
            })
    return results


def detect_power(nets: list[dict]) -> dict:
    """Power rails and grounds, each with its node count."""
    rails, grounds = [], []
    for net in nets:
        leaf = _leaf(net.get("name", ""))
        if not leaf:
            continue
        if any(re.match(p, leaf) for p in GROUND_PATTERNS):
            grounds.append({"name": net.get("name"), "nodes": net.get("node_count", 0)})
            continue
        for pattern, _ in POWER_PATTERNS:
            if re.match(pattern, leaf):
                rails.append({"name": net.get("name"), "leaf": leaf,
                              "nodes": net.get("node_count", 0)})
                break

    rails.sort(key=lambda r: -r["nodes"])
    grounds.sort(key=lambda g: -g["nodes"])
    return {"rails": rails, "grounds": grounds}


def net_statistics(nets: list[dict]) -> dict:
    if not nets:
        return {"total": 0, "unconnected": 0, "single_node": 0, "largest": []}
    single = [n for n in nets if n.get("node_count", 0) == 1]
    largest = sorted(nets, key=lambda n: -n.get("node_count", 0))[:5]
    return {
        "total": len(nets),
        "single_node": len(single),
        "single_node_names": [n.get("name") for n in single[:10]],
        "largest": [{"name": n.get("name"), "nodes": n.get("node_count", 0)} for n in largest],
    }
