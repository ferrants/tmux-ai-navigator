#!/usr/bin/env python3
"""Example extras command: uncommitted changes and ahead/behind counts per session.

  set -g @ainav-extras 'python3 /path/to/git-status-extras.py'

Shows a badge like "✎4" when the session's working tree has uncommitted files, and the
details in the preview. See "Extras" in the README for the protocol.
"""
import json
import re
import subprocess
import sys


def git_status(cwd):
    try:
        out = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "-b"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    lines = out.splitlines()
    head = lines[0] if lines[0].startswith("## ") else ""
    ahead = re.search(r"ahead (\d+)", head)
    behind = re.search(r"behind (\d+)", head)
    return {"dirty": len(lines) - (1 if head else 0),
            "ahead": int(ahead.group(1)) if ahead else 0,
            "behind": int(behind.group(1)) if behind else 0}


def main():
    sessions = json.load(sys.stdin).get("sessions", [])
    by_cwd = {}
    result = {}
    for s in sessions:
        cwd = s.get("cwd")
        if not cwd:
            continue
        if cwd not in by_cwd:
            by_cwd[cwd] = git_status(cwd)
        st = by_cwd[cwd]
        if not st:
            continue
        lines = []
        if st["dirty"]:
            lines.append(f"{st['dirty']} uncommitted file{'s' if st['dirty'] != 1 else ''}")
        if st["ahead"]:
            lines.append(f"{st['ahead']} commit{'s' if st['ahead'] != 1 else ''} not pushed")
        if st["behind"]:
            lines.append(f"{st['behind']} commit{'s' if st['behind'] != 1 else ''} behind upstream")
        info = {"lines": lines, "search": "dirty" if st["dirty"] else ""}
        if st["dirty"]:
            info["badge"], info["badge_style"] = f"✎{st['dirty']}", "info"
        result[s["session_id"]] = info
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
