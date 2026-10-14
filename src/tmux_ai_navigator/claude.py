"""Claude Code sessions, from the live ~/.claude/sessions/<pid>.json files Claude writes.

Each file looks like:
  {"pid": 123, "sessionId": "...", "cwd": "...", "name": "...", "status": "busy|idle|...",
   "statusUpdatedAt": <ms>, "procStart": "<process start token, see proc.py>",
   "pidDomain": "linux:<machine-id>:pid:[ns]", "kind": "interactive"}
These files are undocumented Claude Code internals, so every field is treated as optional.
Files are left behind when Claude exits, so every entry is checked against the process table.
"""
import glob
import json
import os

from . import proc as procmod


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


class SessionFiles:
    """Reads the session files, re-parsing only files whose mtime changed."""

    def __init__(self, directory=None):
        self.dir = directory or os.path.join(config_dir(), "sessions")
        self._cache = {}

    def read(self):
        seen = set()
        out = []
        for path in glob.glob(os.path.join(self.dir, "*.json")):
            seen.add(path)
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                continue
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                data = hit[1]
            else:
                try:
                    with open(path) as f:
                        data = json.load(f)
                    self._cache[path] = (mtime, data)
                except (OSError, ValueError):
                    # caught mid-write (or broken): keep the last good copy and retry next time,
                    # so a session doesn't briefly vanish and lose its observed state
                    data = hit[1] if hit else None
            if isinstance(data, dict):
                out.append(data)
        for path in list(self._cache):
            if path not in seen:
                del self._cache[path]
        return out


def live(entries, table=None, domain=None):
    """Entries whose process is still the one that wrote the file.

    Checks the pid exists and its start time matches procStart (guards against pid reuse),
    and skips entries from another machine/pid namespace. When several live entries claim the
    same session id, the most recently updated wins.
    """
    table = table or procmod.snapshot()
    best = {}
    for d in entries:
        pid, sid = d.get("pid"), d.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or not sid:
            continue
        if d.get("kind", "interactive") != "interactive":
            continue
        if domain and d.get("pidDomain") and d["pidDomain"] != domain:
            continue
        st = table.info(pid)
        if st is None:
            continue
        if d.get("procStart") is not None and st[1] != str(d["procStart"]).strip():
            continue
        key = (pid, sid)
        prev = best.get(key)
        if prev is None or d.get("updatedAt", 0) > prev.get("updatedAt", 0):
            best[key] = d
    return list(best.values())
