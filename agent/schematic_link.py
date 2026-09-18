"""Watch a project's schematic files and report what changed.

**This module never writes a schematic.** eeschema does not implement KiCad
10's IPC API (verified - see docs/SCHEMATIC.md), so there is no way to apply a
change to a running schematic editor. Writing the `.kicad_sch` underneath
eeschema would be silently discarded the next time the user pressed Ctrl+S,
and could destroy their work. Schematic collaboration is therefore
detect-and-review: changes are broadcast, locked and discussed, not applied.

Detection is by file modification time, so a change is visible to the team the
moment the author saves. Parsing a 229 KB schematic takes ~17 ms, so polling
is cheap.
"""
from __future__ import annotations

import logging
import os

from server.schematic.diff import diff_snapshots, summarise
from server.schematic.parser import find_schematic, parse_schematic

log = logging.getLogger("kicadlive.schematic")


class SchematicUnavailable(RuntimeError):
    """No schematic could be found or parsed for this project."""


class SchematicLink:
    """Read-only view of a project's schematic hierarchy."""

    def __init__(self, project_dir: str):
        self.project_dir = os.path.abspath(project_dir)
        self.root_path: str | None = None
        self.baseline: dict[str, dict] = {}
        self._signature: tuple | None = None
        self._watched: list[str] = []

    # ---------------------------------------------------------------- setup

    def connect(self) -> str:
        """Locate and parse the schematic. Returns the root file name."""
        root = find_schematic(self.project_dir)
        if not root:
            raise SchematicUnavailable(
                f"no .kicad_sch found in {self.project_dir}")
        self.root_path = root
        self.baseline = self._parse()
        self._signature = self._current_signature()
        log.info("watching schematic %s (%d objects across %d file(s))",
                 os.path.basename(root), len(self.baseline), len(self._watched))
        return os.path.basename(root)

    @property
    def connected(self) -> bool:
        return self.root_path is not None

    def _parse(self) -> dict[str, dict]:
        objects = parse_schematic(self.root_path)
        self._watched = self._sheet_files()
        return objects

    def _sheet_files(self) -> list[str]:
        """Every .kicad_sch in the project directory.

        Watching the directory rather than only the sheets we parsed means a
        newly added sheet is noticed too.
        """
        try:
            return sorted(
                os.path.join(self.project_dir, f)
                for f in os.listdir(self.project_dir) if f.endswith(".kicad_sch"))
        except OSError:
            return [self.root_path] if self.root_path else []

    def _current_signature(self) -> tuple:
        """(path, mtime, size) for each sheet - cheap change detection."""
        signature = []
        for path in self._sheet_files():
            try:
                stat = os.stat(path)
                signature.append((path, stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(signature)

    def resync(self) -> None:
        """Adopt the files on disk as the new baseline WITHOUT reporting changes.

        Called after SchematicSync replaced a sheet with a teammate's version, so
        their edit is not re-announced as if this user had made it.
        """
        if not self.connected:
            return
        try:
            self.baseline = self._parse()
        except (OSError, ValueError) as exc:
            log.debug("resync could not parse yet (%s)", exc)
            return
        self._signature = self._current_signature()

    # ------------------------------------------------------------ detection

    def changed_on_disk(self) -> bool:
        """True when any schematic file has been saved since the last check."""
        if not self.connected:
            return False
        return self._current_signature() != self._signature

    def poll(self) -> list[dict]:
        """Re-read the schematic and return structured changes since the baseline.

        Returns an empty list when nothing was saved, or when a save produced
        no semantic change (KiCad rewriting formatting must not look like an
        edit).
        """
        if not self.connected:
            return []
        signature = self._current_signature()
        if signature == self._signature:
            return []

        try:
            current = self._parse()
        except (OSError, ValueError) as exc:
            # A save in progress can be caught mid-write. Leave the signature
            # alone so the next poll retries.
            log.debug("schematic not readable yet (%s); will retry", exc)
            return []

        self._signature = signature
        changes = diff_snapshots(self.baseline, current)
        self.baseline = current

        if changes:
            log.info("schematic saved: %d change(s)", len(changes))
        else:
            log.debug("schematic saved with no semantic change")
        return changes

    # -------------------------------------------------------------- lookups

    def snapshot(self) -> dict[str, dict]:
        return dict(self.baseline)

    def object_for_reference(self, reference: str) -> dict | None:
        for obj in self.baseline.values():
            if obj.get("object_type") == "symbol" and obj.get("reference") == reference:
                return obj
        return None

    def sheets(self) -> list[str]:
        return sorted({o.get("sheet", "/") for o in self.baseline.values()})

    def describe(self, change: dict) -> str:
        return summarise(change)

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for obj in self.baseline.values():
            key = obj.get("object_type", "object")
            counts[key] = counts.get(key, 0) + 1
        return {"objects": len(self.baseline), "by_type": counts,
                "sheets": len(self.sheets()), "files": len(self._watched)}


def project_dir_for_board(board_name: str, search_root: str | None = None) -> str | None:
    """Best-effort project directory for an open board.

    KiCad's IPC reports only a board *filename*, not its path, so the agent is
    told where the project lives via --project-dir. This helper resolves the
    common case where the agent is started from the project directory.
    """
    candidate = os.path.abspath(search_root or os.getcwd())
    if not os.path.isdir(candidate):
        return None
    base = os.path.splitext(os.path.basename(board_name or ""))[0]
    if any(f.startswith(base) and f.endswith(".kicad_pcb")
           for f in os.listdir(candidate)):
        return candidate
    return None
