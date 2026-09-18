"""Milestone 0 - verify this machine can run KiCad Live.

    python tools/check_env.py                    (full check, needs KiCad open)
    python tools/check_env.py --server 10.0.0.5  (also check the server is reachable)

Run this on every computer before the demo. It is the fastest way to find the
one machine where the API checkbox was missed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS: list[tuple[bool, str]] = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label))
    print(f"{'[PASS]' if ok else '[FAIL]'} {label:<34}{(' - ' + str(detail)) if detail else ''}",
          flush=True)
    return ok


def warn(label, detail=""):
    print(f"[WARN] {label:<34}{(' - ' + str(detail)) if detail else ''}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="KiCad Live environment check")
    parser.add_argument("--server", default=None, help="sync server IP to test reachability")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    print("=" * 66)
    print(" KiCad Live - environment check")
    print("=" * 66)

    # 1. Python
    version = sys.version_info
    check(version >= (3, 11), "Python 3.11 or newer",
          f"{version.major}.{version.minor}.{version.micro}")

    # 2. Dependencies
    try:
        import kipy  # noqa: F401
        # kipy does not expose __version__, so ask the package metadata.
        try:
            from importlib.metadata import version as _pkg_version
            kipy_version = _pkg_version("kicad-python")
        except Exception:
            kipy_version = "unknown"
        check(True, "kipy importable", f"kicad-python {kipy_version}")
    except ImportError as exc:
        check(False, "kipy importable", f"{exc} - run: pip install -r requirements.txt")
        print("\nInstall dependencies first, then re-run this check.")
        return 1

    try:
        import websockets  # noqa: F401
        check(True, "websockets importable")
    except ImportError as exc:
        check(False, "websockets importable", exc)

    # 3. The API preference
    try:
        from tools.enable_kicad_api import config_dirs
        dirs = config_dirs()
        if dirs:
            path = os.path.join(dirs[-1], "kicad_common.json")
            with open(path, encoding="utf-8") as fh:
                enabled = bool(json.load(fh).get("api", {}).get("enable_server", False))
            check(enabled, "KiCad API enabled in settings",
                  os.path.basename(dirs[-1]) if enabled
                  else "run: python tools/enable_kicad_api.py  (with KiCad closed)")
        else:
            warn("KiCad settings directory", "not found - has KiCad been started once?")
    except Exception as exc:
        warn("KiCad settings readable", exc)

    # 4. The live link
    from agent.kicad_link import KiCadLink, KiCadUnavailable
    link = KiCadLink(client_name_prefix="kl-envcheck")
    try:
        board_name = link.connect()
    except KiCadUnavailable as exc:
        check(False, "Connected to KiCad", exc)
        print("\n  KiCad must be OPEN with a PCB, and the API enabled + KiCad restarted.")
        print("  See docs/TROUBLESHOOTING.md -> 'Agent cannot connect to KiCad'.")
        print("
  If KiCad is open and this still says busy / timed out, KiCad's live API")
        print("  will not work on this computer. That is OK: run the agent with --file-only")
        print("  (it does this automatically) - see docs/LIVE_SCHEMATIC.md.")
        return 1

    check(True, "Connected to KiCad", link.version())
    check(True, "Board open", board_name)

    try:
        snapshot = link.read_snapshot()
    except KiCadUnavailable as exc:
        check(False, "Footprints readable", exc)
        print("
  KiCad answers but will not serve the board (busy / timed out).")
        print("  Use --file-only mode: python -m agent.main --server IP --name NAME --file-only")
        return 1
    check(len(snapshot) > 0, "Footprints readable", f"{len(snapshot)} footprints")
    references = sorted(state["reference"] for state in snapshot.values())
    if references:
        print(f"       {', '.join(references[:12])}{' ...' if len(references) > 12 else ''}")

    # 5. Poll cost - this is what makes live polling viable
    samples = []
    for _ in range(20):
        started = time.perf_counter()
        link.read_snapshot()
        samples.append((time.perf_counter() - started) * 1000)
    average = sum(samples) / len(samples)
    check(average < 100, "Board poll latency",
          f"{average:.1f} ms average, {max(samples):.1f} ms worst")

    # 6. Write access (round-tripped so the board is left untouched)
    try:
        uuid, state = next(iter(snapshot.items()))
        original = state["position"]
        nudged = {"x": round(original["x"] + 0.5, 3), "y": original["y"]}
        link.apply_changes([{"operation": "modify", "uuid": uuid,
                             "reference": state["reference"], "field": "position",
                             "new": nudged}], "KiCad Live env check")
        moved = link.read_snapshot()[uuid]["position"] == nudged
        link.apply_changes([{"operation": "modify", "uuid": uuid,
                             "reference": state["reference"], "field": "position",
                             "new": original}], "KiCad Live env check (undo)")
        restored = link.read_snapshot()[uuid]["position"] == original
        check(moved and restored, "Board is writable",
              f"moved and restored {state['reference']}")
    except Exception as exc:
        check(False, "Board is writable", exc)

    # 7. Server reachability
    if args.server:
        import urllib.request
        url = f"http://{args.server}:{args.port}/health"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read())
            check(payload.get("status") == "ok", "Sync server reachable",
                  f"{url} ({payload.get('clients', 0)} client(s) connected)")
        except Exception as exc:
            check(False, "Sync server reachable", f"{url} - {exc}")
            print("       Check the IP, that the server is running, and the firewall rule.")

    print()
    failed = [label for ok, label in RESULTS if not ok]
    if failed:
        print(f"RESULT: {len(failed)} of {len(RESULTS)} checks FAILED")
        for label in failed:
            print(f"  - {label}")
        return 1
    print(f"RESULT: all {len(RESULTS)} checks PASSED - this machine is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
