"""Install (or remove) the KiCad Live Action Plugin.

    python tools/install_plugin.py              install / update
    python tools/install_plugin.py --uninstall  remove it
    python tools/install_plugin.py --list       show where KiCad looks

Close KiCad first: it only scans for plugins at startup, and it rewrites its
own settings on exit.

The plugin itself is a launcher. This script copies it into KiCad's plugin
directory and records, in kicad_live_config.json, which Python to run the agent
with - normally this repository's .venv.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_SOURCE = os.path.join(ROOT, "plugin")
PACKAGE_NAME = "kicad_live"
COPIED_FILES = ("__init__.py", "kicad_live_plugin.py", "icon.png")


def kicad_documents_root() -> str:
    if sys.platform == "win32":
        documents = os.path.join(os.path.expanduser("~"), "Documents")
        override = os.environ.get("KICAD_DOCUMENTS_HOME")
        return override or os.path.join(documents, "KiCad")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Documents/KiCad")
    return os.path.expanduser("~/.local/share/kicad")


def plugin_dirs() -> list[str]:
    """Every KiCad scripting-plugins directory, oldest version first."""
    root = kicad_documents_root()
    if not os.path.isdir(root):
        return []
    found = []
    for version_dir in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(version_dir):
            continue
        found.append(os.path.join(version_dir, "scripting", "plugins"))
    return found


def venv_python() -> str:
    """The interpreter the agent should run under."""
    candidates = [
        os.path.join(ROOT, ".venv", "Scripts", "python.exe"),   # Windows
        os.path.join(ROOT, ".venv", "bin", "python"),           # Linux / macOS
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    # Fall back to whatever is running this script.
    return sys.executable


def install(target_dir: str) -> bool:
    package_dir = os.path.join(target_dir, PACKAGE_NAME)
    os.makedirs(package_dir, exist_ok=True)

    for name in COPIED_FILES:
        source = os.path.join(PLUGIN_SOURCE, name)
        if os.path.exists(source):
            shutil.copyfile(source, os.path.join(package_dir, name))

    python_exe = venv_python()
    config = {
        "repo_root": ROOT,
        "python_exe": python_exe,
        "installed_by": os.path.basename(__file__),
    }
    with open(os.path.join(package_dir, "kicad_live_config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    print(f"[ OK ] installed to {package_dir}")
    print(f"       repo_root  = {ROOT}")
    print(f"       python_exe = {python_exe}")
    if python_exe == sys.executable and not os.path.exists(os.path.join(ROOT, ".venv")):
        print("[WARN] no .venv found in the repository; the agent will run under")
        print("       the interpreter above. Make sure it has the requirements installed.")
    return True


def uninstall(target_dir: str) -> bool:
    package_dir = os.path.join(target_dir, PACKAGE_NAME)
    if not os.path.isdir(package_dir):
        return False
    shutil.rmtree(package_dir, ignore_errors=True)
    print(f"[ OK ] removed {package_dir}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the KiCad Live action plugin")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--list", action="store_true", help="show plugin directories and exit")
    parser.add_argument("--dir", default=None, help="install into this directory instead")
    args = parser.parse_args()

    targets = [args.dir] if args.dir else plugin_dirs()

    if args.list:
        print("KiCad plugin directories found:")
        for target in targets:
            print(f"  {target}  {'(exists)' if os.path.isdir(target) else '(will be created)'}")
        if not targets:
            print("  none - has KiCad been started at least once?")
        return 0

    if not targets:
        print("[FAIL] No KiCad installation directory found under "
              f"{kicad_documents_root()}")
        print("       Start KiCad once so it creates its folders, then re-run.")
        return 1

    done = 0
    for target in targets:
        if args.uninstall:
            done += 1 if uninstall(target) else 0
        else:
            os.makedirs(target, exist_ok=True)
            done += 1 if install(target) else 0

    print()
    if args.uninstall:
        print(f"Removed from {done} location(s). Restart KiCad.")
    else:
        print(f"Installed into {done} location(s).")
        print("Restart KiCad, then look for:")
        print('  Tools > External Plugins > "KiCad Live - Start collaboration"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
