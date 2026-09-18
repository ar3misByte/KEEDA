"""Schematic FILE sharing: the workaround for eeschema having no live API.

Two SchematicSync agents talk through a fake server that uses the real
SchematicFileStore, each with its own project folder.
"""
import asyncio
import base64
import os

import pytest

from agent.schematic_sync import SchematicSync, sha256_of
from server.schematic_files import SchematicFileError, SchematicFileStore

SHEET = "board.kicad_sch"
V1 = b"(kicad_sch (version 1) (symbol R1 10k))\n"
V2 = b"(kicad_sch (version 1) (symbol R1 4k7))\n"
V3 = b"(kicad_sch (version 1) (symbol R1 22k))\n"


class Hub:
    """Fake server: real store, direct delivery."""

    def __init__(self, tmp_path):
        self.store = SchematicFileStore(str(tmp_path / "srv"))
        self.clients: list["Peer"] = []

    def attach(self, peer):
        self.clients.append(peer)


class Peer:
    """A WSClient stand-in that routes messages through the hub."""

    def __init__(self, hub, tmp_path, name):
        self.hub = hub
        self.name = name
        self.dir = tmp_path / name
        self.dir.mkdir()
        self.connected = asyncio.Event()
        self.connected.set()
        self.banners = []
        self.applied = []
        self.sync = SchematicSync(str(self.dir), self, name, interval=999,
                                  banner=self.banners.append,
                                  on_applied=self.applied.append)
        hub.attach(self)

    def write(self, data: bytes, name=SHEET):
        (self.dir / name).write_bytes(data)

    def read(self, name=SHEET):
        return (self.dir / name).read_bytes()

    async def send(self, payload):
        kind = payload["type"]
        project = "p"
        if kind == "request_schematic_manifest":
            self.sync.on_manifest(self.hub.store.manifest(project))
        elif kind == "schematic_push":
            accepted, rejected = [], []
            for f in payload["files"]:
                try:
                    e = self.hub.store.put(project, f["name"], f["content_b64"], f["sha256"],
                                           f["base_sha256"], self.name)
                    accepted.append({"name": f["name"], "sha256": e["sha256"], "rev": e["rev"]})
                except SchematicFileError as exc:
                    rejected.append({"name": f["name"], "code": exc.code, "reason": str(exc)})
            await self.sync.on_push_result({"accepted": accepted, "rejected": rejected})
            for other in self.hub.clients:
                if other is not self and accepted:
                    other.sync.on_manifest(self.hub.store.manifest(project))
        elif kind == "schematic_pull":
            files = []
            for n in payload["names"]:
                got = self.hub.store.get(project, n)
                if got:
                    data, e = got
                    files.append({"name": n, "sha256": e["sha256"], "author": e["author"],
                                  "rev": e["rev"],
                                  "content_b64": base64.b64encode(data).decode()})
            await self.sync.on_files({"files": files})
        return True


@pytest.fixture(autouse=True)
def no_real_editors(monkeypatch):
    """Never touch real KiCad windows from unit tests."""
    monkeypatch.setattr("agent.schematic_sync.win_ui.AVAILABLE", False)


@pytest.fixture
def two(tmp_path):
    hub = Hub(tmp_path)
    return Peer(hub, tmp_path, "alice"), Peer(hub, tmp_path, "bob"), hub


class TestStore:
    def test_rejects_traversal_and_bad_names(self, tmp_path):
        store = SchematicFileStore(str(tmp_path))
        for bad in ("../evil.kicad_sch", "a/b.kicad_sch", "x.txt", "", None):
            with pytest.raises(SchematicFileError):
                store.put("p", bad, base64.b64encode(V1).decode(), sha256_of(V1), None, "a")

    def test_rejects_corrupt_and_truncated_uploads(self, tmp_path):
        store = SchematicFileStore(str(tmp_path))
        with pytest.raises(SchematicFileError) as e:
            store.put("p", SHEET, base64.b64encode(V1).decode(), "0" * 64, None, "a")
        assert e.value.code == "bad_hash"
        half = b"(kicad_sch (symbol R1"
        with pytest.raises(SchematicFileError) as e:
            store.put("p", SHEET, base64.b64encode(half).decode(), sha256_of(half), None, "a")
        assert e.value.code == "truncated"

    def test_survives_restart(self, tmp_path):
        SchematicFileStore(str(tmp_path)).put(
            "p", SHEET, base64.b64encode(V1).decode(), sha256_of(V1), None, "a")
        again = SchematicFileStore(str(tmp_path))
        assert again.get("p", SHEET)[0] == V1


class TestSharing:
    @pytest.mark.asyncio
    async def test_saved_sheet_reaches_teammate_on_next_cycle(self, two):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()                     # seeds the server
        assert hub.store.get("p", SHEET)[0] == V1

        await bob.sync.cycle()                       # bob had no copy
        assert bob.read() == V1
        assert bob.applied and "File > Revert" in bob.banners[-1]

        alice.write(V2)                              # alice edits and saves
        await alice.sync.push_local()
        await bob.sync.cycle()
        assert bob.read() == V2

    @pytest.mark.asyncio
    async def test_no_echo_after_receiving(self, two):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        rev = hub.store.manifest("p")[SHEET]["rev"]
        await bob.sync.push_local()
        await bob.sync.cycle()
        assert hub.store.manifest("p")[SHEET]["rev"] == rev, "receiving must not re-upload"

    @pytest.mark.asyncio
    async def test_old_file_is_backed_up_before_overwrite(self, two):
        alice, bob, hub = two
        bob.write(V1)
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()                        # identical -> agree silently
        alice.write(V2)
        await alice.sync.push_local()
        await bob.sync.cycle()
        backups = os.listdir(bob.dir / ".kicad_live" / "backup")
        assert len(backups) == 1
        assert (bob.dir / ".kicad_live" / "backup" / backups[0]).read_bytes() == V1

    @pytest.mark.asyncio
    async def test_both_edited_nobody_is_overwritten(self, two):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        alice.write(V2)
        await alice.sync.push_local()                 # alice's edit is shared
        bob.write(V3)                                 # bob edited the OLD version
        await bob.sync.cycle()

        assert bob.read() == V3, "bob's own work must not be overwritten"
        assert (bob.dir / ".kicad_live" / "incoming" / SHEET).read_bytes() == V2
        assert hub.store.get("p", SHEET)[0] == V2, "bob must not clobber alice on the server"
        assert "SAME object" in bob.banners[-1]

    @pytest.mark.asyncio
    async def test_merged_resave_after_conflict_is_shared(self, two):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        alice.write(V2)
        await alice.sync.push_local()
        bob.write(V3)
        await bob.sync.cycle()
        await bob.sync.cycle()                        # unchanged: still no upload
        assert hub.store.get("p", SHEET)[0] == V2

        merged = b"(kicad_sch (version 1) (symbol R1 merged))\n"
        bob.write(merged)                             # user merged and saved
        await bob.sync.push_local()
        assert hub.store.get("p", SHEET)[0] == merged

    @pytest.mark.asyncio
    async def test_first_join_adopts_team_copy_with_backup(self, two):
        alice, bob, hub = two
        alice.write(V2)
        await alice.sync.cycle()
        bob.write(V1)                                 # stale copy, never synced
        await bob.sync.cycle()
        assert bob.read() == V2
        assert os.listdir(bob.dir / ".kicad_live" / "backup")

    @pytest.mark.asyncio
    async def test_half_written_file_is_ignored(self, two):
        alice, bob, hub = two
        alice.write(b"(kicad_sch (symbol R1")
        await alice.sync.cycle()
        assert hub.store.manifest("p") == {}

    @pytest.mark.asyncio
    async def test_read_only_agent_receives_but_never_uploads(self, two):
        alice, bob, hub = two
        bob.sync.read_only = True
        alice.write(V1)
        await alice.sync.cycle()
        bob.write(V2)
        await bob.sync.push_local()
        assert hub.store.get("p", SHEET)[0] == V1

    @pytest.mark.asyncio
    async def test_state_survives_agent_restart(self, two, tmp_path):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        restarted = SchematicSync(str(bob.dir), bob, "bob", interval=999)
        assert restarted.synced[SHEET] == sha256_of(V1)


REAL = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "sample_project", "demo_board.kicad_sch"), encoding="utf-8").read()


class TestMergeInsteadOfConflict:
    @pytest.mark.asyncio
    async def test_edits_to_different_parts_are_merged_and_shared(self, two):
        alice, bob, hub = two
        alice.write(REAL.encode())
        await alice.sync.cycle()
        await bob.sync.cycle()

        alice.write(REAL.replace('"4k7"', '"10k"', 1).encode())   # alice edits one part
        bob.write(REAL.replace('"100nF"', '"1uF"', 1).encode())   # bob edits another
        await alice.sync.push_local()
        await bob.sync.cycle()                                    # bob merges + shares

        merged = bob.read().decode()
        assert '"10k"' in merged and '"1uF"' in merged, "both edits must survive"
        assert bob.sync.stats["merged"] == 1
        assert not (bob.dir / ".kicad_live" / "incoming").exists()

        await alice.sync.cycle()                                  # alice receives the merge
        assert alice.read() == bob.read()
        assert '"10k"' in alice.read().decode() and '"1uF"' in alice.read().decode()


class TestEditorAutomation:
    class FakeEditor:
        def __init__(self, modified=False):
            self.modified = modified
            self.saved = self.reverted = 0

        def is_modified(self):
            return self.modified

        def save(self):
            self.saved += 1
            self.modified = False
            return True

        def revert(self):
            self.reverted += 1
            return True

        def blocked(self):
            return False

    @pytest.mark.asyncio
    async def test_editor_is_reverted_after_a_teammates_file_arrives(self, two, monkeypatch):
        alice, bob, hub = two
        editor = self.FakeEditor()
        monkeypatch.setattr("agent.schematic_sync.win_ui.AVAILABLE", True)
        monkeypatch.setattr("agent.schematic_sync.win_ui.find_editor", lambda k, s="": editor)
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        alice.write(V2)
        await alice.sync.push_local()
        await bob.sync.cycle()
        assert editor.reverted >= 1 and bob.read() == V2
        assert not any("Reload it" in b for b in bob.banners[-1:]), "no manual step needed"

    @pytest.mark.asyncio
    async def test_unsaved_edits_are_saved_first_never_reverted_over(self, two, monkeypatch):
        alice, bob, hub = two
        editor = self.FakeEditor(modified=False)
        monkeypatch.setattr("agent.schematic_sync.win_ui.AVAILABLE", True)
        monkeypatch.setattr("agent.schematic_sync.win_ui.find_editor", lambda k, s="": editor)
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        editor.modified = True
        alice.write(V2)
        await alice.sync.push_local()
        await bob.sync.cycle()
        assert editor.saved == 1, "the user's unsaved edits are saved before anything else"

    @pytest.mark.asyncio
    async def test_without_auto_save_unsaved_edits_block_the_update(self, two, monkeypatch):
        alice, bob, hub = two
        editor = self.FakeEditor(modified=True)
        monkeypatch.setattr("agent.schematic_sync.win_ui.AVAILABLE", True)
        monkeypatch.setattr("agent.schematic_sync.win_ui.find_editor", lambda k, s="": editor)
        bob.sync.auto_save = False
        alice.write(V1)
        await alice.sync.cycle()
        bob.write(V1)
        await bob.sync.cycle()
        alice.write(V2)
        await alice.sync.push_local()
        await bob.sync.cycle()
        assert bob.read() == V1 and editor.reverted == 0 and "unsaved" in bob.banners[-1]


class TestPcbFiles:
    @pytest.mark.asyncio
    async def test_pcb_files_only_travel_when_enabled(self, two):
        alice, bob, hub = two
        pcb = b"(kicad_pcb (version 1) (footprint R1))\n"
        alice.write(pcb, name="board.kicad_pcb")
        await alice.sync.cycle()
        assert hub.store.manifest("p") == {}, "off by default: the KiCad API syncs the PCB"
        alice.sync.sync_pcb = bob.sync.sync_pcb = True
        await alice.sync.cycle()
        await bob.sync.cycle()
        assert bob.read("board.kicad_pcb") == pcb
