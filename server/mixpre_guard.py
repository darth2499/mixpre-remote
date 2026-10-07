#!/usr/bin/env python3
"""
MixPre Remote - update guard. Runs before every start of the bridge
(ExecStartPre). If a just-installed update keeps failing to start, switch back
to the previous version. Deliberately tiny and never replaced by updates.
"""
import json
import os
import time
from pathlib import Path

BASE = Path(os.environ.get("MIXPRE_BASE", "/opt/mixpre-remote"))
MARKER = BASE / "update-pending.json"
MAX_ATTEMPTS = 4          # starts allowed before the update must report healthy
MAX_AGE = 180             # seconds


def point(link_name, target):
    link, tmp = BASE / link_name, BASE / (link_name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


def main():
    if not MARKER.exists():
        return
    try:
        m = json.loads(MARKER.read_text())
    except ValueError:
        m = {}
    m["attempts"] = int(m.get("attempts", 0)) + 1
    age = time.time() - float(m.get("installed_at", time.time()))
    if m["attempts"] <= MAX_ATTEMPTS and age <= MAX_AGE:
        MARKER.write_text(json.dumps(m))
        return
    prev = BASE / "previous"
    if prev.is_symlink() and prev.resolve().exists():
        point("current", str(prev.resolve()))
        prev.unlink()          # nothing sensible to "roll back" to anymore
    (BASE / "update-failed.json").write_text(json.dumps({"version": m.get("version"), "to": m.get("to"), "at": time.time()}))
    MARKER.unlink()
    os.sync()
    print("MixPre Remote: update failed to start - rolled back to the previous version")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block the bridge from starting
        print(f"guard error: {e}")
