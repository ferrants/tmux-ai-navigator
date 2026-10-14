"""Extras: a user-supplied command that adds per-session info to the picker.

Configure it with `set -g @ainav-extras '<command>'` (or --extras). The command gets JSON on
stdin:

  {"sessions": [{"session_id": "...", "pid": 123, "cwd": "...", "status": "idle",
                 "pane_id": "%3", "tmux_session": "work", "window_name": "api"}, ...]}

and prints a JSON object keyed by session id. Every field is optional:

  {"<session_id>": {
      "badge": "85%",          short text shown on the row, before the project tag
      "badge_style": "warn",   "warn" (red), "info" (accent) or "dim" (default)
      "title": "...",          title to use when Claude's own title is missing or weak
      "lines": ["...", ...],   shown in the preview, below the status line
      "search": "..."          extra text matched by search
  }}

It runs at most every `@ainav-extras-interval` seconds (default 5) with a 3 second timeout.
A failing command is reported in the picker's footer and otherwise ignored.
"""
import json
import os
import signal
import subprocess
import time

TIMEOUT = 3


class Extras:
    def __init__(self, command, interval=5.0, runner=None):
        self.command = command
        self.interval = interval
        self.runner = runner or run_command
        self.data = {}
        self.error = ""
        self._at = 0.0
        self._ids = frozenset()

    def get(self, agents, now=None):
        """{session_id: extras dict}; refreshed when stale or when new sessions appear."""
        if not self.command:
            return {}
        now = now or time.time()
        ids = frozenset(a.session_id for a in agents)
        if now - self._at < self.interval and ids <= self._ids:
            return self.data
        self._at, self._ids = now, ids
        payload = {"sessions": [{"session_id": a.session_id, "pid": a.pid, "cwd": a.cwd, "status": a.status,
                                 "pane_id": a.pane_id, "tmux_session": a.tmux_session,
                                 "window_name": a.window_name} for a in agents]}
        try:
            out = self.runner(self.command, json.dumps(payload))
            data = json.loads(out) if out.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object keyed by session id")
            self.data = {k: v for k, v in data.items() if isinstance(v, dict)}
            self.error = ""
        except Exception as e:  # a broken user script must never take the picker down
            self.error = f"extras command failed: {e}"
        return self.data


def run_command(command, stdin):
    # own process group, so a timeout kills the script and anything it started, not just the
    # shell (a surviving child would also hold our pipes open)
    p = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, universal_newlines=True, start_new_session=True)
    try:
        out, err = p.communicate(stdin, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise RuntimeError(f"timed out after {TIMEOUT}s")
    if p.returncode != 0:
        msg = (err.strip().splitlines() or [f"exit {p.returncode}"])[-1]
        raise RuntimeError(msg[:200])
    return out


def apply(agent, info):
    """Copy the fields we understand onto the agent, coercing types defensively."""
    def text(v):
        return v if isinstance(v, str) else "" if v is None else str(v)

    agent.badge = text(info.get("badge"))[:12]
    style = info.get("badge_style")
    agent.badge_style = style if style in ("warn", "info", "dim") else "dim"
    agent.extra_title = text(info.get("title"))
    lines = info.get("lines")
    agent.extra_lines = [text(x) for x in lines][:20] if isinstance(lines, list) else []
    agent.extra_search = text(info.get("search"))
