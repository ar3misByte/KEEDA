"""Near-live schematic (and, optionally, PCB) sharing through the saved files.

eeschema has no live API, so an edit cannot be pushed into another user's open
editor as an object. What CAN be done, and is done here, is the whole loop a
person would otherwise do by hand:

    author edits  ->  editor saved (auto)  ->  file uploaded  ->  server tells
    everyone  ->  each agent MERGES it into the local file  ->  the editor is
    reloaded (auto File > Revert)

End to end this takes a few seconds. What this cannot do is show a change
BEFORE the author's editor saves it: the edit only exists in that editor's
memory until then. Auto-save (default 3 s after the first unsaved edit) keeps
that gap small.

Safety rules - never destroy someone's work:
  * Files are compared by sha256 against the last version both sides agreed on.
  * If both sides changed a file, the changes are MERGED object by object
    (common/sexpr_merge.py). Only a genuine clash on the same object falls back
    to writing the teammate's copy to `.kicad_live/incoming/`.
  * The old file is always backed up first; writes are atomic.
  * An editor with unsaved edits is saved first (auto-save) or, if auto-save is
    off, left alone and retried - it is never reverted over.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time

from agent import win_ui
from common.sexpr_merge import merge
from server.schematic_files import MAX_FILE_BYTES, valid_name

log = logging.getLogger("kicadlive.schematic_sync")

DEFAULT_INTERVAL = 30.0         # fallback refresh; normal updates are pushed by the server
STATE_DIR = ".kicad_live"
BACKUPS_KEPT = 20


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _kind(name: str) -> str:
    return "pcb" if name.endswith(".kicad_pcb") else "sch"


class SchematicSync:
    def __init__(self, project_dir: str, ws, user_name: str, read_only: bool = False,
                 interval: float = DEFAULT_INTERVAL, banner=None, on_applied=None,
                 sync_pcb: bool = False, auto_save: bool = True, auto_reload: bool = True,
                 autosave_delay: float = 3.0):
        self.project_dir = os.path.abspath(project_dir)
        self.ws = ws
        self.user_name = user_name
        self.read_only = read_only
        self.interval = interval
        self.banner = banner or (lambda text: log.info(text))
        self.on_applied = on_applied            # called after a file was replaced
        self.sync_pcb = sync_pcb
        self.auto_save = auto_save
        self.auto_reload = auto_reload
        self.autosave_delay = autosave_delay

        self.manifest: dict[str, dict] = {}     # the server's latest, name -> entry
        self.synced: dict[str, str] = self._load_state()   # name -> last agreed sha256
        self._diverged: dict[str, str] = {}     # name -> local sha we refused to overwrite
        self._handled_remote: dict[str, str] = {}   # name -> remote sha already dealt with
        self._pushing: dict[str, tuple[str, bytes]] = {}    # name -> (sha, content) in flight
        self._unsaved_since: dict[str, float] = {}
        self._signature: dict[str, tuple] = {}
        self._manifest_seen = asyncio.Event()
        self._force = asyncio.Event()
        self._lock = asyncio.Lock()             # one apply/push at a time
        self.stats = {"pushed": 0, "pulled": 0, "merged": 0, "diverged": 0,
                      "reloaded": 0, "autosaved": 0}

    # ---------------------------------------------------------------- state

    @property
    def _state_dir(self) -> str:
        return os.path.join(self.project_dir, STATE_DIR)

    def _wanted(self, name: str) -> bool:
        return valid_name(name) and (self.sync_pcb or _kind(name) == "sch")

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

    def _base_path(self, name: str) -> str:
        return os.path.join(self._state_dir, "base", name)

    def _remember_base(self, name: str, data: bytes) -> None:
        """Keep the last agreed content: it is the common ancestor for merging."""
        try:
            os.makedirs(os.path.dirname(self._base_path(name)), exist_ok=True)
            self._atomic_write(self._base_path(name), data)
        except OSError as exc:
            log.warning("could not store merge base for %s: %s", name, exc)

    def _load_base(self, name: str) -> bytes | None:
        try:
            with open(self._base_path(name), "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        return data if sha256_of(data) == self.synced.get(name) else None

    def local_files(self) -> dict[str, str]:
        """name -> sha256 of each complete top-level file we share."""
        found: dict[str, str] = {}
        try:
            names = os.listdir(self.project_dir)
        except OSError:
            return found
        for name in names:
            if not self._wanted(name):
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
        # Someone shared something new: refresh right away instead of on the timer.
        local = self.local_files()
        for name, entry in self.manifest.items():
            if self._wanted(name) and entry.get("sha256") != local.get(name) \
                    and self._handled_remote.get(name) != entry.get("sha256"):
                self._force.set()
                break

    def on_reconnect(self) -> None:
        self._pushing.clear()
        self._force.set()

    async def on_push_result(self, message: dict) -> None:
        for item in message.get("accepted") or []:
            name = item.get("name")
            sha, data = self._pushing.pop(name, (item.get("sha256"), None))
            self.synced[name] = item.get("sha256") or sha
            if data is not None:
                self._remember_base(name, data)
            # The server does not echo our own push back, so keep our view of the
            # team's copy current or the next save would use a stale base.
            self.manifest[name] = {"sha256": self.synced[name], "rev": item.get("rev"),
                                   "author": self.user_name}
            self.stats["pushed"] += 1
            log.info("shared %s with the team", name)
        for item in message.get("rejected") or []:
            self._pushing.pop(item.get("name"), None)
            if item.get("code") == "stale_base":
                log.info("%s: a teammate saved first - fetching and merging", item.get("name"))
                self._force.set()
            elif item.get("code") != "truncated":
                log.warning("server refused %s: %s", item.get("name"), item.get("reason"))
        self._save_state()

    async def on_files(self, message: dict) -> None:
        async with self._lock:
            applied = []
            for item in message.get("files") or []:
                try:
                    if await self._apply(item):
                        applied.append(item)
                except Exception:
                    log.exception("could not apply %s", item.get("name"))
            self._save_state()
        if applied:
            await self.push_local()        # a merge may contain our edits the team lacks

    # ------------------------------------------------------------------ push

    async def push_local(self) -> None:
        """Upload files saved since the last sync. Cheap; safe to call often."""
        if self.read_only or not self.ws.connected.is_set():
            return
        upload = []
        for name, sha in self.local_files().items():
            remote = self.manifest.get(name)
            if remote is not None and remote.get("sha256") == sha:
                if self.synced.get(name) != sha:
                    self.synced[name] = sha          # already identical: agree silently
                    data = self._read(name)
                    if data is not None:
                        self._remember_base(name, data)
                self._diverged.pop(name, None)
                continue
            if self._pushing.get(name, (None,))[0] == sha:
                continue
            base = self.synced.get(name)
            if remote is None:
                base = None                          # first upload of this file
            elif base is None:
                continue                             # first join: the pull adopts theirs
            elif base != sha and remote.get("sha256") != base:
                # Both sides changed and the merge could not resolve it. Only an
                # explicit re-save after the user has merged may overwrite the team.
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
            self._pushing[name] = (sha, data)
        if not upload:
            return
        if not await self.ws.send({"type": "schematic_push", "files": upload}):
            self._pushing.clear()

    # ------------------------------------------------------------------ pull

    async def pull_remote(self) -> None:
        """Ask for every file where the team's copy differs from ours."""
        local = self.local_files()
        wanted = [name for name, entry in self.manifest.items()
                  if self._wanted(name) and local.get(name) != entry.get("sha256")
                  and self._handled_remote.get(name) != entry.get("sha256")]
        if wanted:
            await self.ws.send({"type": "schematic_pull", "names": wanted})

    async def _apply(self, item: dict) -> bool:
        """Bring one downloaded file into the project. True if the live file changed."""
        name = item.get("name")
        if not self._wanted(name):
            return False
        theirs = base64.b64decode(item.get("content_b64") or "", validate=True)
        sha = sha256_of(theirs)
        if sha != item.get("sha256") or not theirs.rstrip().endswith(b")"):
            log.warning("discarding corrupted download of %s", name)
            return False
        author = item.get("author") or "a teammate"
        path = os.path.join(self.project_dir, name)
        editor = await self._editor(name)

        # An editor with unsaved edits: get them onto disk first so the merge
        # includes them, or leave everything alone. Never revert over them.
        if editor is not None and editor.is_modified():
            if not self.auto_save or not await asyncio.to_thread(editor.save):
                self.banner(f"{name}: {author} shared a newer version but you have unsaved "
                            "edits. Save (Ctrl+S) and it will be merged automatically.")
                return False
            self.stats["autosaved"] += 1

        local = self._read(name) if os.path.exists(path) else None
        local_sha = sha256_of(local) if local is not None else None
        if local_sha == sha:
            self.synced[name] = sha
            self._remember_base(name, theirs)
            self._handled_remote[name] = sha
            return False

        known = self.synced.get(name)
        unmodified = not os.path.exists(path) or known is None or local_sha == known
        new_content, how = theirs, "replaced"
        if not unmodified:
            base = self._load_base(name)
            result = merge(base.decode("utf-8", "replace"), local.decode("utf-8", "replace"),
                           theirs.decode("utf-8", "replace")) if base and local else None
            if result is not None and result.clean:
                new_content, how = result.text.encode("utf-8"), "merged"
                self.stats["merged"] += 1
            else:
                incoming = os.path.join(self._state_dir, "incoming")
                os.makedirs(incoming, exist_ok=True)
                target = os.path.join(incoming, name)
                self._atomic_write(target, theirs)
                self._diverged[name] = local_sha or ""
                self._handled_remote[name] = sha
                self.stats["diverged"] += 1
                what = ", ".join(result.conflicts[:3]) if result is not None else "no common base"
                self.banner(f"{name}: {author} and you changed the SAME object ({what}). Your "
                            f"file was NOT touched. Their version: {target}. Copy over what "
                            "you need into yours and save; your save is then shared.")
                return False

        if local is not None:
            self._backup(name, local)
        self._atomic_write(path, new_content)
        self.synced[name] = sha                # the team's version is now our base
        self._remember_base(name, theirs)
        self._handled_remote[name] = sha
        self._diverged.pop(name, None)
        self.stats["pulled"] += 1
        if self.on_applied is not None:
            # Adopt the new file as the baseline NOW (no await since the write), or
            # the schematic watcher reports the teammate's edit as ours.
            self.on_applied([name])
        await self._reload(name, author, how)
        return True

    async def _editor(self, name: str):
        if not win_ui.AVAILABLE:
            return None
        stem = os.path.splitext(name)[0]
        return await asyncio.to_thread(win_ui.find_editor, _kind(name), stem)

    async def _reload(self, name: str, author: str, how: str) -> None:
        """Make the open editor show the new file (File > Revert), if it is safe."""
        editor = await self._editor(name)
        if editor is not None and self.auto_reload:
            if editor.is_modified():
                pass                                   # edited again meanwhile: do not discard
            elif await asyncio.to_thread(editor.revert):
                self.stats["reloaded"] += 1
                log.info("%s %s from %s and reloaded in the editor", name, how, author)
                return
        self.banner(f"{name} was {how} with {author}'s changes in your project folder. "
                    "Reload it in the editor: File > Revert (or close and reopen).")

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
            for stale in sorted(os.listdir(folder))[:-BACKUPS_KEPT]:
                os.remove(os.path.join(folder, stale))
        except OSError as exc:
            log.warning("could not back up %s: %s", name, exc)

    # ------------------------------------------------------------------ loops

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
        watchers = [asyncio.create_task(self._watch_files())]
        if self.auto_save and win_ui.AVAILABLE and not self.read_only:
            watchers.append(asyncio.create_task(self._autosave_loop()))
        try:
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
        finally:
            for task in watchers:
                task.cancel()

    async def _watch_files(self) -> None:
        """Upload a file about a second after it is saved, whoever saved it."""
        while True:
            await asyncio.sleep(1.0)
            try:
                signature = {}
                for name in self.local_files():
                    st = os.stat(os.path.join(self.project_dir, name))
                    signature[name] = (st.st_mtime_ns, st.st_size)
                if signature != self._signature:
                    self._signature = signature
                    async with self._lock:
                        await self.push_local()
            except Exception:
                log.exception("file watcher failed")

    async def _autosave_loop(self) -> None:
        """Save an editor that has held unsaved edits for `autosave_delay` seconds.

        This is what turns "edit" into "shared": eeschema keeps edits only in
        memory until it saves. A dialog open in the editor pauses this.
        """
        while True:
            await asyncio.sleep(1.0)
            try:
                for kind in (("sch", "pcb") if self.sync_pcb else ("sch",)):
                    editor = await asyncio.to_thread(win_ui.find_editor, kind, "")
                    if editor is None:
                        continue
                    if not editor.is_modified():
                        self._unsaved_since.pop(kind, None)
                        continue
                    first = self._unsaved_since.setdefault(kind, time.time())
                    if time.time() - first >= self.autosave_delay and \
                            not await asyncio.to_thread(editor.blocked):
                        if await asyncio.to_thread(editor.save):
                            self.stats["autosaved"] += 1
                            log.info("auto-saved the %s editor to share your edit", kind)
                        self._unsaved_since.pop(kind, None)
            except Exception:
                log.exception("auto-save failed")


FileSync = SchematicSync
