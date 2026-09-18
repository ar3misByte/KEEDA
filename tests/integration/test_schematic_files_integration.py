"""Schematic file sharing through the real server over real WebSockets."""
from __future__ import annotations

import base64
import hashlib

import pytest

from tests.integration.test_collab_integration import pair, server  # noqa: F401

DATA = b"(kicad_sch (version 1) (symbol R1 10k))\n"


def push(data, name="t.kicad_sch", base=None):
    return {"type": "schematic_push", "files": [{
        "name": name, "sha256": hashlib.sha256(data).hexdigest(), "base_sha256": base,
        "content_b64": base64.b64encode(data).decode()}]}


@pytest.mark.asyncio
async def test_push_is_announced_and_pullable_and_persisted(server):
    a, b = await pair(server, "files_a", "Aditya", "Rahul")
    try:
        await a.send(push(DATA))
        result = await a.wait_for("schematic_push_result")
        assert result["accepted"] and not result["rejected"]

        manifest = await b.wait_for("schematic_manifest",
                                    predicate=lambda m: "t.kicad_sch" in m["files"])
        assert manifest["files"]["t.kicad_sch"]["author"] == "Aditya"

        await b.send({"type": "schematic_pull", "names": ["t.kicad_sch"]})
        got = (await b.wait_for("schematic_files"))["files"][0]
        assert base64.b64decode(got["content_b64"]) == DATA

        late = await pair(server, "files_a", "Late")          # joins afterwards
        assert late[0] is not None
        m = await late[0].wait_for("schematic_manifest")
        assert "t.kicad_sch" in m["files"], "a late joiner must learn about existing sheets"
        await late[0].close()
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_stale_push_and_bad_name_are_rejected(server):
    a, b = await pair(server, "files_b", "Aditya", "Rahul")
    try:
        await a.send(push(DATA))
        await a.wait_for("schematic_push_result")
        newer = DATA.replace(b"10k", b"4k7")
        await b.send(push(newer, base=None))                   # b never saw a's version
        assert (await b.wait_for("schematic_push_result"))["rejected"][0]["code"] == "stale_base"

        await b.send(push(DATA, name="../escape.kicad_sch"))
        assert (await b.wait_for("schematic_push_result"))["rejected"][0]["code"] == "bad_name"
    finally:
        await a.close()
        await b.close()
