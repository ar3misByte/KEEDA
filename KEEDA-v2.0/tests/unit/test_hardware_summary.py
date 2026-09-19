"""Unit tests for the offline hardware-summary analyzer.

The most important tests here are the NEGATIVE ones. An analyzer that reports
an interface nobody designed is worse than one that reports nothing, so these
check that unsupported claims are never made.
"""
import os

import pytest

from server.project_analysis.component_analyzer import (
    analyze_components, classify_designator,
)
from server.project_analysis.net_analyzer import (
    detect_interfaces, detect_power, net_statistics,
)
from server.project_analysis.netlist import find_kicad_cli, parse_netlist_xml
from server.project_analysis.summary import ProjectAnalyzer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE_DIR = os.path.join(ROOT, "sample_project")

needs_kicad = pytest.mark.skipif(find_kicad_cli() is None,
                                 reason="kicad-cli not installed on this machine")


def net(name, *nodes):
    """nodes are (ref, pin, pinfunction) triples."""
    return {"name": name, "code": "1",
            "nodes": [{"reference": r, "pin": p, "function": f, "type": ""}
                      for r, p, f in nodes],
            "node_count": len(nodes)}


class TestInterfaceDetection:
    def test_i2c_from_net_names(self):
        found = detect_interfaces([net("/SDA", ("U1", "1", "")), net("/SCL", ("U1", "2", ""))])
        assert [i["name"] for i in found] == ["I2C"]
        assert found[0]["confidence"] == "confirmed"

    def test_i2c_from_pin_functions(self):
        """KiCad appends the pin number, e.g. SDA_5; both forms must match."""
        found = detect_interfaces([
            net("/DATA", ("U1", "5", "SDA_5")),
            net("/CLOCK", ("U1", "6", "SCL_6")),
        ])
        assert [i["name"] for i in found] == ["I2C"]

    def test_evidence_names_where_it_matched(self):
        found = detect_interfaces([
            net("/DATA", ("U1", "5", "SDA_5")),
            net("/CLOCK", ("U1", "6", "SCL_6")),
        ])
        evidence = " ".join(found[0]["evidence"])
        assert "U1.5" in evidence and "SDA_5" in evidence, \
            "evidence must say WHY it matched, not just name a net"

    def test_half_an_interface_is_not_enough(self):
        assert detect_interfaces([net("/SDA", ("U1", "1", ""))]) == []

    def test_uart_needs_both_directions(self):
        assert detect_interfaces([net("/TX", ("U1", "1", ""))]) == []
        found = detect_interfaces([net("/TX", ("U1", "1", "")), net("/RX", ("U1", "2", ""))])
        assert [i["name"] for i in found] == ["UART"]

    def test_spi_needs_three_of_four(self):
        assert detect_interfaces([net("/MOSI", ("U1", "1", "")),
                                  net("/MISO", ("U1", "2", ""))]) == []
        found = detect_interfaces([net("/MOSI", ("U1", "1", "")),
                                   net("/MISO", ("U1", "2", "")),
                                   net("/SCK", ("U1", "3", ""))])
        assert [i["name"] for i in found] == ["SPI"]

    def test_usb_is_not_claimed_without_evidence(self):
        """The specific false claim called out in the brief."""
        found = detect_interfaces([net("/SDA", ("U1", "1", "")), net("/SCL", ("U1", "2", "")),
                                   net("+3V3", ("U1", "3", "")), net("GND", ("U1", "4", ""))])
        assert "USB" not in [i["name"] for i in found]

    def test_a_usb_capable_part_alone_proves_nothing(self):
        found = detect_interfaces([net("/GPIO1", ("U1", "1", "")),
                                   net("/GPIO2", ("U1", "2", ""))])
        assert found == []

    def test_usb_detected_when_evidence_exists(self):
        found = detect_interfaces([net("/USB_D+", ("J1", "1", "")),
                                   net("/USB_D-", ("J1", "2", ""))])
        assert "USB" in [i["name"] for i in found]

    def test_empty_netlist_yields_nothing(self):
        assert detect_interfaces([]) == []


class TestPowerDetection:
    def test_rails_and_grounds_split(self):
        power = detect_power([net("+3V3", ("U1", "1", "")), net("GND", ("U1", "2", "")),
                              net("/SDA", ("U1", "3", ""))])
        assert [r["leaf"] for r in power["rails"]] == ["+3V3"]
        assert [g["name"] for g in power["grounds"]] == ["GND"]

    def test_signal_nets_are_not_power(self):
        power = detect_power([net("/DATA", ("U1", "1", ""))])
        assert power["rails"] == [] and power["grounds"] == []

    def test_hierarchical_names_use_the_leaf(self):
        power = detect_power([net("/sheet/VCC", ("U1", "1", ""))])
        assert power["rails"] and power["rails"][0]["leaf"] == "VCC"


class TestComponentClassification:
    def test_designator_prefixes(self):
        assert classify_designator("R1") == "Resistors"
        assert classify_designator("C12") == "Capacitors"
        assert classify_designator("U3") == "Integrated circuits"
        assert classify_designator("J2") == "Connectors"

    def test_led_beats_inductor(self):
        assert classify_designator("LED1") == "LEDs"
        assert classify_designator("L1") == "Inductors"

    def test_mcu_family_recognised(self):
        result = analyze_components([
            {"reference": "U1", "value": "ESP32-WROOM-32", "part": "ESP32",
             "library": "RF", "description": "", "footprint": "mod"}])
        assert result["microcontrollers"][0]["family"] == "ESP32"

    def test_no_mcu_claimed_for_a_passive_board(self):
        result = analyze_components([
            {"reference": "R1", "value": "10k", "part": "R", "library": "Device",
             "description": "", "footprint": "0805"},
            {"reference": "C1", "value": "100nF", "part": "C", "library": "Device",
             "description": "", "footprint": "0805"}])
        assert result["microcontrollers"] == []

    def test_bom_groups_by_value_and_footprint(self):
        result = analyze_components([
            {"reference": "R1", "value": "10k", "part": "R", "library": "", "description": "",
             "footprint": "0805"},
            {"reference": "R2", "value": "10k", "part": "R", "library": "", "description": "",
             "footprint": "0805"}])
        assert result["bom_lines"][0]["quantity"] == 2
        assert result["distinct_values"] == 1

    def test_missing_footprints_are_reported(self):
        result = analyze_components([
            {"reference": "R1", "value": "10k", "part": "R", "library": "",
             "description": "", "footprint": ""}])
        assert result["unpopulated_footprints"] == ["R1"]


class TestNetlistParsing:
    def test_malformed_xml_is_rejected(self):
        from server.project_analysis.netlist import NetlistUnavailable
        with pytest.raises(NetlistUnavailable):
            parse_netlist_xml("<not-valid")

    def test_minimal_document(self):
        xml = """<?xml version="1.0"?><export version="E">
          <components><comp ref="R1"><value>10k</value>
            <libsource lib="Device" part="R" description="Resistor"/>
          </comp></components>
          <nets><net code="1" name="/SDA"><node ref="R1" pin="1" pinfunction="SDA"/></net></nets>
        </export>"""
        parsed = parse_netlist_xml(xml)
        assert parsed["components"][0]["reference"] == "R1"
        assert parsed["nets"][0]["node_count"] == 1


class TestNetStatistics:
    def test_counts_single_node_nets(self):
        stats = net_statistics([net("/A", ("U1", "1", "")),
                                net("/B", ("U1", "2", ""), ("U2", "1", ""))])
        assert stats["total"] == 2 and stats["single_node"] == 1


@needs_kicad
class TestEndToEndSummary:
    """Runs the whole analyzer against the shipped sample project."""

    @pytest.fixture(scope="class")
    def summary(self):
        return ProjectAnalyzer().summarise(SAMPLE_DIR, force=True)

    def test_summary_is_produced_offline(self, summary):
        assert summary["available"] is True
        assert summary["offline"] is True

    def test_all_expected_components_found(self, summary):
        refs = {c["reference"] for c in summary["schematic"]["component_list"]}
        assert {"R1", "R2", "C1", "C2", "U1", "J1"} <= refs

    def test_i2c_detected_because_the_project_has_it(self, summary):
        assert "I2C" in [i["name"] for i in summary["schematic"]["interfaces"]]

    def test_usb_not_claimed(self, summary):
        """The sample board has no USB; the analyzer must not invent it."""
        assert "USB" not in [i["name"] for i in summary["schematic"]["interfaces"]]

    def test_no_microcontroller_claimed(self, summary):
        """The sample board is a sensor + header; there is no MCU on it."""
        assert summary["schematic"]["components"]["microcontrollers"] == []
        mcu_line = next(l for l in summary["headline"] if l["label"] == "Microcontroller")
        assert mcu_line["value"] is None

    def test_power_rails_reported(self, summary):
        rails = [r["leaf"] for r in summary["schematic"]["power"]["rails"]]
        assert any("3V3" in r for r in rails)

    def test_board_facts_present(self, summary):
        board = summary["pcb"]
        assert board["copper_layer_count"] == 2
        assert board["footprint_count"] == 6
        assert board["dimensions_mm"]["width"] > 0

    def test_unknowns_are_none_not_invented(self, summary):
        for line in summary["headline"]:
            assert line["value"] is None or isinstance(line["value"], str)
            if line["value"] is None:
                assert line["evidence"], "an unknown must explain why it is unknown"


@needs_kicad
class TestNoNetworkAccess:
    """Prove the summary needs no network, rather than asserting it.

    Rather than physically unplugging the machine, this makes every outbound
    socket raise. If any code path tried to reach a service, the summary would
    fail loudly here.
    """

    def test_summary_works_with_all_sockets_blocked(self, monkeypatch):
        import socket as socket_module

        class Blocked(socket_module.socket):
            def __init__(self, *args, **kwargs):
                raise OSError("network access is not allowed in this test")

        def blocked_connection(*args, **kwargs):
            raise OSError("network access is not allowed in this test")

        monkeypatch.setattr(socket_module, "socket", Blocked)
        monkeypatch.setattr(socket_module, "create_connection", blocked_connection)

        summary = ProjectAnalyzer().summarise(SAMPLE_DIR, force=True)

        assert summary["available"] is True
        assert summary["offline"] is True
        assert summary["schematic"]["component_count"] == 6
        assert "I2C" in [i["name"] for i in summary["schematic"]["interfaces"]]

    def test_analysis_modules_import_no_network_libraries(self):
        """A cheap guard against someone adding an HTTP call later."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2] / "server" / "project_analysis"
        banned = ("import requests", "import httpx", "import urllib.request",
                  "from urllib", "openai", "anthropic", "googleapis")
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for needle in banned:
                assert needle not in text, f"{path.name} references {needle!r}"
