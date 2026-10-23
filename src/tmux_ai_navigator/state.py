"""Persistent seen/later state, shared by the picker and the tmux hooks.

  servers[pid]      per tmux server (pane ids are only unique within one server):
    seen[pane_id]   last time the pane was on screen in any client (epoch seconds)
    visible         panes on screen at the last update, so leaving a pane also counts as seeing it
    created         first use; sessions that finished before this aren't reported as "done"
    back            the picker view to come back to after a jump (see views.py)
    recent[tty]     {panes, at}: panes that client looked at, most recent first (prefix A)
  later[session_id] when the user flagged a Claude session to come back to

Records of servers that are gone are dropped, so a restarted tmux starts fresh.

Read-modify-write happens under a lock on a separate file, and the state file is replaced
atomically, so the hooks and an open picker can update it concurrently.
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "tmux-ai-navigator")


class State:
    def __init__(self, directory=None):
        self.dir = directory or state_dir()
        self.path = os.path.join(self.dir, "state.json")
        self.lock_path = os.path.join(self.dir, "state.lock")

    def read(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        if not isinstance(d.get("servers"), dict):
            d["servers"] = {}
        if not isinstance(d.get("later"), dict):
            d["later"] = {}
        if "server" in d:  # single-server layout of earlier versions
            if isinstance(d.get("seen"), dict) and str(d["server"]) not in d["servers"]:
                d["servers"][str(d["server"])] = {"seen": d["seen"], "visible": d.get("visible") or [],
                                                   "created": d.get("created", 0)}
        for old in ("seen", "visible", "server", "created"):
            d.pop(old, None)
        return d

    @contextmanager
    def update(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            d = self.read()
            yield d
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)


def server_view(d, server, now=None):
    """This server's record (read-only use). Unknown server: nothing seen, tracking starts now."""
    v = d.get("servers", {}).get(str(server))
    return v if isinstance(v, dict) else {"seen": {}, "visible": [], "created": now or time.time()}


def mark_visible(d, server, visible_now, now=None, extra_seen=(), alive=None):
    """Record what's on screen. Panes visible now or at the last update count as seen now.
    `alive(server)` drops records of servers that are gone."""
    now = now or time.time()
    servers = d.setdefault("servers", {})
    if alive:
        for other in [s for s in servers if s != str(server)]:
            if not alive(other):
                del servers[other]
    v = servers.setdefault(str(server), {"seen": {}, "visible": [], "created": now})
    for pane in set(visible_now) | set(v.get("visible", [])) | set(extra_seen):
        v["seen"][pane] = max(now, v["seen"].get(pane, 0))  # monotonic: hooks may finish out of order
    v["visible"] = sorted(set(visible_now))


RECENT_PANES = 20
RECENT_CLIENTS = 16


def _recent(d, server):
    v = d.get("servers", {}).get(str(server))
    r = v.get("recent") if isinstance(v, dict) else None
    return r if isinstance(r, dict) else {}


def focus_changed(d, server, focus):
    r = _recent(d, server)
    return any(not isinstance(r.get(tty), dict) or (r[tty].get("panes") or [None])[0] != pane
               for tty, pane in (focus or {}).items())


def record_focus(d, server, focus, now=None):
    """Put each client's current pane first in its history."""
    now = now or time.time()
    v = d.setdefault("servers", {}).setdefault(str(server), {"seen": {}, "visible": [], "created": now})
    r = v.get("recent") if isinstance(v.get("recent"), dict) else {}
    for tty, pane in focus.items():
        e = r.get(tty) if isinstance(r.get(tty), dict) else {}
        panes = [p for p in (e.get("panes") or []) if isinstance(p, str) and p != pane]
        r[tty] = {"panes": ([pane] + panes)[:RECENT_PANES], "at": now}
    keep = sorted(r, key=lambda t: r[t].get("at", 0) if isinstance(r[t], dict) else 0, reverse=True)
    v["recent"] = {t: r[t] for t in keep[:RECENT_CLIENTS]}


def previous_pane(d, server, tty, current, candidates):
    """The pane looked at most recently before `current` that's in candidates (agent panes):
    from this client's history, else from other clients', most recent first."""
    r = _recent(d, server)
    others = sorted((t for t in r if t != tty), key=lambda t: r[t].get("at", 0) if isinstance(r[t], dict) else 0,
                    reverse=True)
    for t in ([tty] if tty in r else []) + others:
        e = r[t] if isinstance(r[t], dict) else {}
        for p in e.get("panes") or []:
            if p != current and p in candidates:
                return p
    return None


def toggle_later(d, session_id, since=None, now=None):
    """Flag or unflag. A stored flag that has already expired (the session changed status
    after it was set, see later_active) counts as unflagged, so one press flags it again."""
    ts = d["later"].get(session_id)
    if ts and not (since and since > ts + 1):
        del d["later"][session_id]
        return False
    d["later"][session_id] = now or time.time()
    return True


def is_done(agent, view, visible_now):
    """Finished a turn since the user last looked at its pane.

    A session counts once it has been idle after real work (statusUpdatedAt well after the
    process started, so a freshly started session isn't "done"), its pane isn't on screen
    right now, and that change came after the pane was last seen (or after first use).
    """
    if agent.status != "idle" or not agent.since or not agent.pane_id:
        return False
    if agent.pane_id in visible_now:
        return False
    if agent.started and agent.since - agent.started < 10:
        return False
    seen = view["seen"].get(agent.pane_id, view.get("created", 0))
    return agent.since > seen


def later_active(agent, d):
    """Flag stays until the session changes status after being flagged (i.e. you prompted it)."""
    ts = d["later"].get(agent.session_id)
    return bool(ts) and not (agent.since and agent.since > ts + 1)
