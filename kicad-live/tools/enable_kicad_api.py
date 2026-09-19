"""Turn on KiCad's IPC API server (the setting KiCad Live depends on).

KiCad rewrites its config when it exits, so CLOSE KICAD before running this,
or your change will be overwritten.

    python tools/enable_kicad_api.py
    python tools/enable_kicad_api.py --check      (report only, change nothing)

The equivalent manual step is:
    KiCad -> Preferences -> Plugins -> tick "Enable KiCad API" -> restart KiCad
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys


def config_dirs() -> list[str]:
    """Every KiCad settings directory on this machine, newest version last."""
    if sys.platform == "win32":
        root = os.path.join(os.environ.get("APPDATA", ""), "kicad")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Preferences/kicad")
    else:
        root = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "kicad")

    if not os.path.isdir(root):
        return []
    found = [d for d in glob.glob(os.path.join(root, "*"))
             if os.path.isfile(os.path.join(d, "kicad_common.json"))]
    if os.path.isfile(os.path.join(root, "kicad_common.json")):
        found.append(root)

    def version_key(path: str):
        name = os.path.basename(path)
        try:
            return tuple(int(part) for part in name.split("."))
        except ValueError:
            return (0,)

    return sorted(found, key=version_key)


def kicad_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import subprocess
        output = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=15).stdout.lower()
        return "kicad.exe" in output or "pcbnew.exe" in output
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Enable the KiCad IPC API server")
    parser.add_argument("--check", action="store_true", help="report the current setting only")
    args = parser.parse_args()

    dirs = config_dirs()
    if not dirs:
        print("[FAIL] No KiCad configuration directory found.")
        print("       Start KiCad once so it creates its settings, then re-run this.")
        return 1

    if not args.check and kicad_running():
        print("[WARN] KiCad appears to be RUNNING.")
        print("       KiCad rewrites its settings when it closes, which would undo this change.")
        print("       Close KiCad completely, then run this again.")
        return 1

    changed = 0
    for directory in dirs:
        path = os.path.join(directory, "kicad_common.json")
        version = os.path.basename(directory)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                settings = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[FAIL] KiCad {version}: could not read {path} ({exc})")
            continue

        current = bool(settings.get("api", {}).get("enable_server", False))

        if args.check:
            print(f"[{'PASS' if current else 'FAIL'}] KiCad {version}: enable_server = {current}")
            continue

        if current:
            print(f"[ OK ] KiCad {version}: API server already enabled")
            continue

        backup = path + ".kicadlive-backup"
        if not os.path.exists(backup):
            shutil.copyfile(path, backup)

        settings.setdefault("api", {})["enable_server"] = True
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, indent=2)
        except OSError as exc:
            print(f"[FAIL] KiCad {version}: could not write settings ({exc})")
            continue

        print(f"[ OK ] KiCad {version}: API server ENABLED  (backup: {os.path.basename(backup)})")
        changed += 1

    if args.check:
        return 0

    print()
    if changed:
        print("Now start KiCad and open a PCB. The agent can then connect.")
    else:
        print("Nothing to change. Start KiCad and open a PCB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
