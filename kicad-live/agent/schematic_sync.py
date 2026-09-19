"""Share saved schematic files between team members, on a timer.

eeschema has no live API, so another user's edit can never be pushed into an
open schematic editor. This module is the workaround: files instead of
objects.

  * When you SAVE a sheet, the agent uploads the `.kicad_sch` to the server.
  * Every `interval` seconds (and when it connects) the agent asks the server
    what the team's latest sheets are and writes newer ones into YOUR project
    folder. You then reload the sheet in eeschema (File > Revert, or reopen).

Safety rules - the whole design is about never destroying someone's work:

  * A sheet is only overwritten if it is unchanged since the last sync (its
    sha256 equals the recorded one). The old file is always backed up first.
  * If you edited the sheet AND someone else did, neither wins silently: their
    copy goes to `.kicad_live/incoming/` and your file is left untouched.
  * A push carries the version it was based on; the server rejects stale ones.
  * Writes are atomic (temp file + rename), so eeschema never sees half a file.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time

from server.schematic_files import MAX_FILE_BYTES, valid_name

log = logging.getLogger("kicadlive.schematic_sync")

DEFAULT_INTERVAL = 120.0        # seconds between team refreshes
STATE_DIR = ".kicad_live"
BACKUPS_KEPT = 20


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SchematicSync:
    def __init__(self, project_dir: str, ws, user_name: str, read_only: bool = False,
                 interval: float = DEFAULT_INTERVAL, banner=None, on_applied=None):
        self.project_dir = os.path.abspath(project_dir)
        self.ws = ws
        self.user_name = user_name
        self.read_only = read_only
        self.interval = interval
        self.banner = banner or (lambda text: log.info(text))
        self.on_applied = on_applied            # called (sync) after a file is replaced

        self.manifest: dict[str, dict] = {}     # the server's latest, name -> entry
        self.synced: dict[str, str] = self._load_state()   # name -> last agreed sha256
        self._diverged: dict[str, str] = {}     # name -> local sha we refused to overwrite
        self._pushing: dict[str, str] = {}      # name -> sha with an upload in flight
        self._manifest_seen = asyncio.Event()
        self._force = asyncio.Event()
        self.stats = {"pushed": 0, "pulled": 0, "diverged": 0}

    # ---------------------------------------------------------------- state

    @property
    def _state_dir(self) -> str:
        return os.path.join(self.project_dir, STATE_DIR)

    def _load_state(self) -> dict[str, str]:
        try:
            with open(os.path.join(self._state_dir, "sync_state.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            path = os.path.join(self._state_dir, "sync_state.json")
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(self.synced, fh, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as exc:
            log.warning("could not save sync state: %s", exc)

    def local_files(self) -> dict[str, str]:
        """name -> sha256 of each complete top-level .kicad_sch."""
        found: dict[str, str] = {}
        try:
            names = os.listdir(self.project_dir)
        except OSError:
            return found
        for name in names:
            if not valid_name(name):
                continue
            data = self._read(name)
            if data is not None:
                found[name] = sha256_of(data)
        return found

    def _read(self, name: str) -> bytes | None:
        try:
            with open(os.path.join(self.project_dir, name), "rb") as fh:
                data = fh.read(MAX_FILE_BYTES + 1)
        except OSError:
            return None
        # A save caught half-way ends mid-expression; treat it as unreadable.
        if not data or len(data) > MAX_FILE_BYTES or not data.rstrip().endswith(b")"):
            return None
        return data

    # ----------------------------------------------------------- inbound msgs

    def on_manifest(self, files: dict) -> None:
        self.manifest = files if isinstance(files, dict) else {}
        self._manifest_seen.set()

    def on_reconnect(self) -> None:
        self._pushing.clear()
        self._force.set()

    async def on_push_result(self, message: dict) -> None:
        for item in message.get("accepted") or []:
            name = item.get("name")
            sha = self._pushing.pop(name, None) or item.get("sha256")
            self.synced[name] = item.get("sha256") or sha
            # The server does not echo our own push back to us, so keep our view of
            # the team's copy current or the next save would use a stale base.
            self.manifest[name] = {"sha256": self.synced[name], "rev": item.get("rev"),
                                   "author": self.user_name}
            self.stats["pushed"] += 1
            log.info("shared %s with the team", name)
        for item in message.get("rejected") or []:
            self._pushing.pop(item.get("name"), None)
            if item.get("code") == "stale_base":
                self.banner(f"{item.get('name')}: a teammate saved this sheet before you. "
                            "Your version was NOT shared; their copy is being fetched.")
                self._force.set()
            elif item.get("code") != "truncated":
                log.warning("server refused %s: %s", item.get("name"), item.get("reason"))
        self._save_state()

    async def on_files(self, message: dict) -> None:
        applied = []
        for item in message.get("files") or []:
            try:
                if self._apply(item):
                    applied.append(item)
            except Exception:
                log.exception("could not apply %s", item.get("name"))
        self._save_state()
        if applied and self.on_applied is not None:
            self.on_applied([i["name"] for i in applied])

    # ------------------------------------------------------------------ push

    async def push_local(self) -> None:
        """Upload sheets saved since the last sync. Cheap; safe to call often."""
        if self.read_only or not self.ws.connected.is_set():
            return
        upload = []
        for name, sha in self.local_files().items():
            remote = self.manifest.get(name)
            if remote is not None and remote.get("sha256") == sha:
                if self.synced.get(name) != sha:
                    self.synced[name] = sha          # already identical: agree silently
                self._diverged.pop(name, None)
                continue
            if self._pushing.get(name) == sha:
                continue
            base = self.synced.get(name)
            if remote is None:
                base = None                          # first upload of this sheet
            elif base is None:
                continue                             # first join: the pull adopts theirs
            elif base != sha and remote.get("sha256") != base:
                # Both sides changed. Only an explicit re-save after the user has
                # merged may overwrite the team's copy.
                if self._diverged.get(name, sha) == sha:
                    continue
                base = remote["sha256"]
                self._diverged.pop(name, None)
            elif base == sha:
                continue                             # unchanged since last sync
            data = self._read(name)
            if data is None or sha256_of(data) != sha:
                continue
            upload.append({"name": name, "sha256": sha, "base_sha256": base,
                           "content_b64": base64.b64encode(data).decode("ascii")})
        if not upload:
            return
        for item in upload:
            self._pushing[item["name"]] = item["sha256"]
        if not await self.ws.send({"type": "schematic_push", "files": upload}):
            self._pushing.clear()

    # ------------------------------------------------------------------ pull

    async def pull_remote(self) -> None:
        """Ask for every sheet where the team's copy differs from ours."""
        local = self.local_files()
        wanted = [name for name, entry in self.manifest.items()
                  if valid_name(name) and local.get(name) != entry.get("sha256")]
        if wanted:
            await self.ws.send({"type": "schematic_pull", "names": wanted})

    def _apply(self, item: dict) -> bool:
        """Place one downloaded sheet on disk. True if the live file was replaced."""
        name = item.get("name")
        if not valid_name(name):
            return False
        data = base64.b64decode(item.get("content_b64") or "", validate=True)
        sha = sha256_of(data)
        if sha != item.get("sha256") or not data.rstrip().endswith(b")"):
            log.warning("discarding corrupted download of %s", name)
            return False

        path = os.path.join(self.project_dir, name)
        author = item.get("author") or "a teammate"
        local_data = self._read(name) if os.path.exists(path) else None
        local_sha = sha256_of(local_data) if local_data is not None else None

        if local_sha == sha:
            self.synced[name] = sha
            return False

        unmodified = (local_data is None and not os.path.exists(path)) \
            or self.synced.get(name) is None or local_sha == self.synced.get(name)
        if not unmodified:
            incoming = os.path.join(self._state_dir, "incoming")
            os.makedirs(incoming, exist_ok=True)
            target = os.path.join(incoming, name)
            self._atomic_write(target, data)
            self._diverged[name] = local_sha or ""
            self.stats["diverged"] += 1
            self.banner(f"{name}: {author} and you BOTH changed this sheet. Your file was "
                        f"NOT touched. Their version is saved at {target} - open it, copy "
                        "over what you need into yours, and save.")
            return False

        if local_data is not None:
            self._backup(name, local_data)
        self._atomic_write(path, data)
        self.synced[name] = sha
        self._diverged.pop(name, None)
        self.stats["pulled"] += 1
        self.banner(f"{name} was updated by {author} and written to your project folder. "
                    "In eeschema use File > Revert (or close and reopen the sheet) to see "
                    "it. Reload BEFORE editing or saving, or your save will be treated as "
                    "a conflicting edit.")
        return True

    def _atomic_write(self, path: str, data: bytes) -> None:
        tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.kl-tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    def _backup(self, name: str, data: bytes) -> None:
        try:
            folder = os.path.join(self._state_dir, "backup")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}_{name}"), "wb") as fh:
                fh.write(data)
            old = sorted(os.listdir(folder))
            for stale in old[:-BACKUPS_KEPT]:
                os.remove(os.path.join(folder, stale))
        except OSError as exc:
            log.warning("could not back up %s: %s", name, exc)

    # ------------------------------------------------------------------ loop

    async def cycle(self) -> None:
        """One full refresh: fresh manifest, upload our saves, download theirs."""
        self._manifest_seen.clear()
        await self.ws.send({"type": "request_schematic_manifest"})
        try:
            await asyncio.wait_for(self._manifest_seen.wait(), 5.0)
        except asyncio.TimeoutError:
            return                                   # server not answering; retry later
        await self.push_local()
        await self.pull_remote()

    async def run(self) -> None:
        while True:
            await self.ws.connected.wait()
            self._force.clear()
            try:
                await self.cycle()
            except Exception:
                log.exception("schematic sync cycle failed")
            try:
                await asyncio.wait_for(self._force.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
