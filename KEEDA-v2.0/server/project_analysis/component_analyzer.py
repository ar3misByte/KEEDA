"""Classify components from their own reference designators and libraries.

Reference designators are a standing engineering convention (R = resistor,
C = capacitor, U = integrated circuit), so they are real evidence. Anything
beyond that - "this is the microcontroller" - is only claimed when the part
name or library says so.
"""
from __future__ import annotations

import re
from collections import Counter

# Reference-designator prefixes, per the usual convention.
DESIGNATOR_CLASSES = [
    (r"^R", "Resistors"), (r"^C", "Capacitors"), (r"^L", "Inductors"),
    (r"^D", "Diodes"), (r"^LED", "LEDs"), (r"^Q", "Transistors"),
    (r"^U", "Integrated circuits"), (r"^IC", "Integrated circuits"),
    (r"^J", "Connectors"), (r"^P", "Connectors"), (r"^CN", "Connectors"),
    (r"^SW", "Switches"), (r"^S", "Switches"), (r"^BT", "Batteries"),
    (r"^Y", "Crystals/oscillators"), (r"^X", "Crystals/oscillators"),
    (r"^F", "Fuses"), (r"^K", "Relays"), (r"^T", "Transformers"),
    (r"^TP", "Test points"), (r"^M", "Mounting/mechanical"),
    (r"^A", "Sub-assemblies"),
]

# Families we will name explicitly, matched against value / part / description.
MCU_PATTERNS = [
    (r"\bESP32[\w-]*", "ESP32"), (r"\bESP8266\b", "ESP8266"),
    (r"\bSTM32[\w]*", "STM32"), (r"\bATMEGA\s?\d+\w*", "ATmega"),
    (r"\bATTINY\s?\d+\w*", "ATtiny"), (r"\bRP2040\b", "RP2040"),
    (r"\bPIC\s?\d{2}\w*", "PIC"), (r"\bNRF\d+\w*", "nRF"),
    (r"\bSAMD\d+\w*", "SAMD"), (r"\bMSP430\w*", "MSP430"),
]

REGULATOR_PATTERNS = [
    r"\bAMS1117\b", r"\bLM\d{3,4}\b", r"\bAP\d{4}\b", r"\bMCP\d{4}\b",
    r"\bLD\d{2,4}\b", r"\bTPS\d+\w*\b", r"\bXC\d{4}\b", r"\b78\d{2}\b", r"\b79\d{2}\b",
    r"\bREGULATOR\b", r"\bLDO\b", r"\bBUCK\b", r"\bBOOST\b",
]

SENSOR_PATTERNS = [
    r"\bBME\d+\b", r"\bBMP\d+\b", r"\bDHT\d+\b", r"\bMPU\d+\b", r"\bDS18B20\b",
    r"\bSHT\d+\b", r"\bHTU\d+\b", r"\bTMP\d+\b", r"\bADXL\d+\b", r"\bLIS\d\w+\b",
    r"\bSENSOR\b", r"\bTHERMISTOR\b",
]

CONNECTOR_PATTERNS = [
    (r"\bUSB\b", "USB"), (r"\bDB9\b", "DB9 serial"), (r"\bRJ45\b", "RJ45"),
    (r"\bHDMI\b", "HDMI"), (r"\bSD[_-]?CARD\b", "SD card"),
    (r"\bJST\b", "JST"), (r"\bBARREL\b", "Barrel jack"),
    (r"\bHEADER\b", "Pin header"), (r"\bSCREW[_-]?TERM\w*\b", "Screw terminal"),
]


def _haystack(component: dict) -> str:
    return " ".join([
        component.get("value", ""), component.get("part", ""),
        component.get("library", ""), component.get("description", ""),
        component.get("footprint", ""),
    ]).upper()


def classify_designator(reference: str) -> str:
    ref = (reference or "").upper()
    prefix = re.match(r"^[A-Z]+", ref)
    if not prefix:
        return "Other"
    # Longest designator prefixes first, so LED beats L.
    for pattern, label in sorted(DESIGNATOR_CLASSES, key=lambda p: -len(p[0])):
        if re.match(pattern, ref):
            return label
    return "Other"


def _match_any(haystack: str, patterns) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        if isinstance(pattern, tuple):
            regex, label = pattern
            if re.search(regex, haystack) and label not in found:
                found.append(label)
        else:
            match = re.search(pattern, haystack)
            if match and match.group(0) not in found:
                found.append(match.group(0))
    return found


def analyze_components(components: list[dict]) -> dict:
    """Inventory plus any part families the project's own text supports."""
    inventory = Counter()
    by_class: dict[str, list[str]] = {}
    for component in components:
        label = classify_designator(component.get("reference", ""))
        inventory[label] += 1
        by_class.setdefault(label, []).append(component.get("reference", ""))

    microcontrollers, regulators, sensors, connectors = [], [], [], []
    for component in components:
        haystack = _haystack(component)
        reference = component.get("reference", "")
        value = component.get("value", "") or component.get("part", "")

        for pattern, family in MCU_PATTERNS:
            match = re.search(pattern, haystack)
            if match:
                microcontrollers.append({"reference": reference, "family": family,
                                         "part": match.group(0), "value": value})
                break

        if _match_any(haystack, REGULATOR_PATTERNS):
            regulators.append({"reference": reference, "value": value})
        if _match_any(haystack, SENSOR_PATTERNS):
            sensors.append({"reference": reference, "value": value})

        kinds = _match_any(haystack, CONNECTOR_PATTERNS)
        if kinds and classify_designator(reference) == "Connectors":
            connectors.append({"reference": reference, "kind": kinds[0], "value": value})

    # Distinct values are what a BOM actually groups on.
    bom = Counter()
    for component in components:
        bom[(component.get("value", ""), component.get("footprint", ""))] += 1
    bom_lines = [{"value": value, "footprint": footprint, "quantity": quantity}
                 for (value, footprint), quantity in
                 sorted(bom.items(), key=lambda kv: -kv[1])]

    return {
        "total": len(components),
        "inventory": [{"category": category, "count": count,
                       "references": sorted(by_class[category])[:40]}
                      for category, count in inventory.most_common()],
        "microcontrollers": microcontrollers,
        "regulators": regulators,
        "sensors": sensors,
        "connectors": connectors,
        "bom_lines": bom_lines[:60],
        "distinct_values": len(bom),
        "unpopulated_footprints": [c.get("reference") for c in components
                                   if not c.get("footprint")][:40],
    }
