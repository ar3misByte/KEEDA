"""KiCad Live action plugin package.

KiCad imports this package from its plugins directory at startup.

Two things happen here beyond the import:

1. Any exception is caught. A plugin that raises during import can stop KiCad
   from finishing its plugin scan, which would hide OTHER people's plugins too.
2. The outcome is written to `last_load.json` next to this file. KiCad does not
   show Python import errors anywhere obvious, so this file is the fastest way
   to answer "why is the menu entry missing?" - see docs/TROUBLESHOOTING.md.
"""
import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _record(status, detail=""):
    try:
        with open(os.path.join(_HERE, "last_load.json"), "w", encoding="utf-8") as fh:
            json.dump({"status": status, "detail": detail,
                       "when": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
    except OSError:
        pass


try:
    from .kicad_live_plugin import KiCadLivePlugin  # noqa: F401
    _record("loaded")
except Exception as _exc:  # pragma: no cover - diagnostics only
    import traceback
    _record("failed", traceback.format_exc())
    print("KiCad Live plugin failed to load: %s" % _exc)
    traceback.print_exc()
