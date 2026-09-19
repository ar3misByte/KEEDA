r"""Verify the KiCad Live Action Plugin is installed and loadable.

Run with KiCad's bundled Python:

    & "C:\Program Files\KiCad\10.0\bin\python.exe" tests\manual\test_plugin_loads.py

What this proves and what it does not:

* It DOES prove the installed plugin imports cleanly under the exact
  interpreter KiCad uses, that the class builds, that `defaults()` fills in the
  menu entry, and that the recorded repo/python paths still exist.
* It does NOT call `register()`. That only works inside the running KiCad
  application - standalone it trips a `PgmOrNull()` assertion in KiCad's own
  C++ code. The final confirmation is visual:
      Tools > External Plugins > "KiCad Live - Start collaboration"
"""
import glob
import json
import os
import sys

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label))
    print("%s %s%s" % ("[PASS]" if ok else "[FAIL]", label, (" - " + str(detail)) if detail else ""))
    return ok


def find_installed():
    documents = os.path.join(os.path.expanduser("~"), "Documents", "KiCad")
    matches = glob.glob(os.path.join(documents, "*", "scripting", "plugins", "kicad_live"))
    if not matches:
        matches = glob.glob(os.path.expanduser("~/.local/share/kicad/*/scripting/plugins/kicad_live"))
    return matches


def main():
    print("=" * 62)
    print(" KiCad Live - plugin install check")
    print("=" * 62)

    try:
        import pcbnew
        check(True, "pcbnew importable", pcbnew.GetBuildVersion())
    except ImportError as exc:
        check(False, "pcbnew importable", "%s (use KiCad's bundled python.exe)" % exc)
        return 1

    installed = find_installed()
    if not check(bool(installed), "Plugin is installed",
                 installed[0] if installed else "run: python tools/install_plugin.py"):
        return 1

    package_dir = installed[0]
    for name in ("__init__.py", "kicad_live_plugin.py", "kicad_live_config.json"):
        check(os.path.exists(os.path.join(package_dir, name)), "Contains %s" % name)

    config_path = os.path.join(package_dir, "kicad_live_config.json")
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError) as exc:
        check(False, "Config readable", exc)
        return 1

    check(os.path.isdir(config.get("repo_root", "")), "repo_root exists",
          config.get("repo_root"))
    check(os.path.exists(config.get("python_exe", "")), "python_exe exists",
          config.get("python_exe"))

    # Import the installed module the same way KiCad will.
    plugins_root = os.path.dirname(package_dir)
    sys.path.insert(0, plugins_root)
    try:
        import wx
        if wx.GetApp() is None:
            wx.App(False)
        check(True, "wxPython available", wx.version())
    except Exception as exc:
        check(False, "wxPython available", exc)
        return 1

    # Import the plugin module directly. Importing the package would run
    # register(), which needs the KiCad application to exist.
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "kicad_live_plugin_probe", os.path.join(package_dir, "kicad_live_plugin.py"))
        module = importlib.util.module_from_spec(spec)
        # Stop the module-level register() call from aborting the import.
        original_register = pcbnew.ActionPlugin.register
        pcbnew.ActionPlugin.register = lambda self: None
        try:
            spec.loader.exec_module(module)
        finally:
            pcbnew.ActionPlugin.register = original_register
        check(True, "Plugin module imports cleanly")
    except Exception as exc:
        check(False, "Plugin module imports cleanly", exc)
        import traceback
        traceback.print_exc()
        return 1

    try:
        plugin = module.KiCadLivePlugin()
        plugin.defaults()
        check(plugin.name == "KiCad Live - Start collaboration",
              "Menu entry name set", plugin.name)
        check(plugin.category == "Collaboration", "Category set", plugin.category)
        check(hasattr(plugin, "Run"), "Run() implemented")
    except Exception as exc:
        check(False, "Plugin class usable", exc)
        return 1

    print()
    failed = [label for ok, label in RESULTS if not ok]
    if failed:
        print("RESULT: %d/%d checks FAILED" % (len(failed), len(RESULTS)))
        for label in failed:
            print("  - " + label)
        return 1
    print("RESULT: all %d checks PASSED" % len(RESULTS))
    print()
    print("Final step is visual: start KiCad, open a PCB, and look for")
    print('  Tools > External Plugins > "KiCad Live - Start collaboration"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
