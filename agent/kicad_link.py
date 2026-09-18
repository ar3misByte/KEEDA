"""The only module that talks to KiCad.

Everything that depends on the KiCad IPC API lives here, so if a future KiCad
release changes the API this is the one file to fix. Verified against
KiCad 10.0.6 with kicad-python 0.8.0 (see docs/ENVIRONMENT.md).

Notes on the API that were learned the hard way:

* ``Vector2(x, y)`` does NOT work - it takes a protobuf. Use
  ``Vector2.from_xy(x_nm, y_nm)``. Same for ``Angle.from_degrees``.
* KiCad tracks an open commit **per client name**. If a client begins a commit
  and dies, every later connection using that same name is refused with
  "already has a commit in progress", and only restarting KiCad clears it. So
  every commit here is wrapped in try/finally, and each agent run uses a unique
  client name.
"""
from __future__ import annotations

import logging
import uuid as uuidlib

from common.diff_engine import mm_to_nm, nm_to_mm, normalise_rotation

log = logging.getLogger("kicadlive.kicad")


class KiCadUnavailable(RuntimeError):
    """KiCad is not running, the API is disabled, or no board is open."""


class KiCadBusy(KiCadUnavailable):
    """KiCad is running but cannot answer right now.

    KiCad replies "busy" while a modal dialog is open, while the user is in the
    middle of an interactive tool (dragging a part), or while it is loading a
    board. That is TRANSIENT: nothing is wrong and nothing should be torn down.
    Callers must wait and retry rather than treat it as a lost connection.
    Subclasses KiCadUnavailable so old `except KiCadUnavailable` still works.
    """


def _is_busy(exc: Exception) -> bool:
    """True for errors that mean 'try again shortly', not 'KiCad is gone'."""
    code = getattr(exc, "code", None)
    try:
        from kipy.proto.common import ApiStatusCode
        busy_codes = {ApiStatusCode.AS_BUSY, ApiStatusCode.AS_TIMEOUT,
                      ApiStatusCode.AS_NOT_READY}
    except Exception:
        busy_codes = {7, 2, 4}
    text = str(exc).lower()
    return (code in busy_codes or "busy" in text
            or "timed out" in text or "timeout" in text)


def _translate(exc: Exception, what: str) -> KiCadUnavailable:
    cls = KiCadBusy if _is_busy(exc) else KiCadUnavailable
    return cls(f"{what}: {exc}")


class KiCadLink:
    def __init__(self, client_name_prefix: str = "kicad-live"):
        # A per-run suffix guarantees a crashed previous run can never block us
        # with a leaked commit.
        self.client_name = f"{client_name_prefix}-{uuidlib.uuid4().hex[:8]}"
        self._kicad = None
        self._board = None
        self._layer_names: dict[int, str] = {}
        self._layer_ids: dict[str, int] = {}

    # ---------------------------------------------------------------- connect

    @property
    def connected(self) -> bool:
        return self._board is not None

    def connect(self) -> str:
        """Attach to the running KiCad. Returns the open board's name."""
        try:
            from kipy import KiCad
        except ImportError as exc:
            raise KiCadUnavailable(
                "the 'kicad-python' package is not installed (pip install -r requirements.txt)"
            ) from exc

        try:
            self._kicad = KiCad(client_name=self.client_name)
            self._board = self._kicad.get_board()
        except Exception as exc:
            self._kicad = self._board = None
            if _is_busy(exc):
                raise KiCadBusy(f"KiCad is busy ({exc})") from exc
            raise KiCadUnavailable(
                f"could not reach KiCad ({exc}). Is KiCad running with a PCB open, "
                f"and is 'Enable KiCad API' ticked in Preferences > Plugins?"
            ) from exc

        self._cache_layers()
        name = self._board.name
        log.info("connected to KiCad as %s, board '%s'", self.client_name, name)
        return name

    def disconnect(self) -> None:
        self._kicad = self._board = None

    def version(self) -> str:
        try:
            return str(self._kicad.get_version())
        except Exception:
            return "unknown"

    def _cache_layers(self) -> None:
        self._layer_names.clear()
        self._layer_ids.clear()
        # Only the layers a footprint can actually sit on matter to us.
        for layer_id in range(0, 64):
            try:
                name = self._board.get_layer_name(layer_id)
            except Exception:
                continue
            if name:
                self._layer_names[layer_id] = name
                self._layer_ids.setdefault(name, layer_id)

    def layer_name(self, layer_id: int) -> str:
        if layer_id in self._layer_names:
            return self._layer_names[layer_id]
        try:
            name = self._board.get_layer_name(layer_id)
        except Exception:
            name = str(layer_id)
        self._layer_names[layer_id] = name
        return name

    # ------------------------------------------------------------------- read

    def read_snapshot(self) -> dict[str, dict]:
        """Every footprint on the live board, normalised for diffing."""
        if self._board is None:
            raise KiCadUnavailable("not connected to KiCad")
        try:
            footprints = self._board.get_footprints()
        except Exception as exc:
            raise _translate(exc, "could not read the board") from exc

        snapshot: dict[str, dict] = {}
        for footprint in footprints:
            try:
                snapshot[footprint.id.value] = self._to_state(footprint)
            except Exception:
                log.debug("skipping an unreadable footprint", exc_info=True)
        return snapshot

    def _to_state(self, footprint) -> dict:
        position = footprint.position
        return {
            "uuid": footprint.id.value,
            "reference": footprint.reference_field.text.value,
            "object_type": "footprint",
            "position": {"x": nm_to_mm(position.x), "y": nm_to_mm(position.y)},
            "rotation": normalise_rotation(footprint.orientation.degrees),
            "layer": self.layer_name(footprint.layer),
            "value": footprint.value_field.text.value,
        }

    def read_selection(self) -> list[str]:
        """References of the currently selected footprints."""
        if self._board is None:
            return []
        try:
            selection = self._board.get_selection()
        except Exception as exc:
            raise _translate(exc, "could not read the selection") from exc
        references: list[str] = []
        for item in selection:
            try:
                reference = item.reference_field.text.value
            except AttributeError:
                continue          # not a footprint (a track, a zone, ...)
            if reference:
                references.append(reference)
        return references

    def selection_uuids(self) -> dict[str, str]:
        """{uuid: reference} for the current selection."""
        if self._board is None:
            return {}
        # Must raise on failure. Returning {} would read as "the user deselected
        # everything" and make the agent release every lock they hold.
        try:
            selection = self._board.get_selection()
        except Exception as exc:
            raise _translate(exc, "could not read the selection") from exc
        result: dict[str, str] = {}
        for item in selection:
            try:
                result[item.id.value] = item.reference_field.text.value
            except AttributeError:
                continue
        return result

    # ------------------------------------------------------------------ write

    def apply_changes(self, changes: list[dict], description: str = "KiCad Live") -> int:
        """Apply remote changes to the live board inside one undoable commit.

        Returns the number of footprints actually modified. Unknown UUIDs are
        skipped rather than treated as an error: the other designer may be
        editing a part this board does not have.
        """
        if self._board is None:
            raise KiCadUnavailable("not connected to KiCad")

        wanted: dict[str, list[dict]] = {}
        for change in changes:
            if change.get("operation") != "modify":
                continue          # add/remove of footprints is out of MVP scope
            uuid = change.get("uuid")
            if uuid:
                wanted.setdefault(uuid, []).append(change)
        if not wanted:
            return 0

        try:
            footprints = {f.id.value: f for f in self._board.get_footprints()}
        except Exception as exc:
            raise _translate(exc, "could not read the board") from exc

        targets = []
        for uuid, object_changes in wanted.items():
            footprint = footprints.get(uuid)
            if footprint is None:
                log.debug("remote change for unknown footprint %s, skipped", uuid[:8])
                continue
            # NOTE: build the list first. `any(generator)` would short-circuit
            # after the first successful mutation and silently drop the rest,
            # so a combined move+rotate would only ever move.
            applied = [self._mutate(footprint, change) for change in object_changes]
            if any(applied):
                targets.append(footprint)

        if not targets:
            return 0

        commit = self._board.begin_commit()
        pushed = False
        try:
            self._board.update_items(targets)
            self._board.push_commit(commit, description)
            pushed = True
        except Exception as exc:
            raise _translate(exc, "failed to apply a change to KiCad") from exc
        finally:
            if not pushed:
                # Never leave a commit open: it would poison this client name
                # in KiCad until KiCad itself is restarted.
                try:
                    self._board.drop_commit(commit)
                except Exception:
                    log.warning("could not drop the failed commit", exc_info=True)

        log.info("applied %d change(s) to %d footprint(s)", len(changes), len(targets))
        return len(targets)

    def _mutate(self, footprint, change: dict) -> bool:
        """Set one field on a footprint object. Returns True if it changed."""
        from kipy.geometry import Angle, Vector2

        field = change.get("field")
        value = change.get("new")
        try:
            if field == "position" and isinstance(value, dict):
                footprint.position = Vector2.from_xy(mm_to_nm(value["x"]), mm_to_nm(value["y"]))
                return True
            if field == "rotation":
                footprint.orientation = Angle.from_degrees(float(value))
                return True
            if field == "layer":
                layer_id = self._layer_ids.get(str(value))
                if layer_id is None:
                    log.warning("unknown layer %r, change skipped", value)
                    return False
                footprint.layer = layer_id
                return True
            if field == "value":
                footprint.value_field.text.value = str(value)
                return True
            if field == "reference":
                footprint.reference_field.text.value = str(value)
                return True
        except Exception:
            log.warning("could not set %s on %s", field, change.get("reference"), exc_info=True)
            return False
        return False
