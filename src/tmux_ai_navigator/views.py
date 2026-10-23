"""Picker tabs (scopes) and the "↺ back" view: where you were when you last jumped.

A scope is {"kind": "all" | "session" | "project", "value": ..., "label": ...}:
  session  agents with a pane in that tmux session (by id; linked windows count in each session)
  project  agents whose folder is in that project (git root, or the folder itself outside git)

The back view is saved per tmux server after a jump from the picker and offered as the first
tab when the picker opens again soon after: same scope (pinned to what it meant then, not to
where you are now), search, grouping, OLDER expanded, and the cursor on the row after the one
you jumped to.
"""
import math
import os
import re
import time

from .agents import Agent

KINDS = ("all", "session", "project")
MODES = ("priority", "project")
DEFAULT_RESUME_MIN = 10.0
MAX_QUERY = 200
ALL = {"kind": "all", "value": "", "label": "all"}
OLDER = ("more", "older")


def is_repo(d):
    g = os.path.join(d, ".git")
    if os.path.isdir(g):
        return os.path.isfile(os.path.join(g, "HEAD"))
    try:
        with open(g) as f:  # worktree or submodule
            return f.read(200).startswith("gitdir:")
    except OSError:
        return False


def project_root(path, _cache={}):
    """The git root above path (a worktree counts as its own root), else path itself.
    No git subprocess: walks up looking for .git, like agents.git_branch. A repo at ~ or /
    (dotfiles) doesn't make everything below it one project."""
    if not path:
        return ""
    path = os.path.abspath(os.path.expanduser(path))
    hit = _cache.get(path)
    if hit is not None:
        return hit
    root = path
    stop = {os.path.expanduser("~"), os.path.dirname(path) if path == "/" else "/"}
    if os.path.isdir(path):
        d = path
        while True:
            if d in stop and d != path:
                break
            if is_repo(d):
                root = d
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    _cache[path] = root
    return root


def short_path(path):
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def session_label(name):
    """Like Agent.tag: "2api" -> "api"."""
    return re.sub(r"^\d+[-_.]?", "", name or "") or name or "?"


def opener_scopes(session_id=None, session_name=None, cwd=None):
    """Tabs for where the picker was opened from: all, its tmux session, its project."""
    scopes = [dict(ALL)]
    if session_id:
        scopes.append({"kind": "session", "value": session_id, "label": session_label(session_name or session_id)})
    root = project_root(cwd)
    if root:
        scopes.append({"kind": "project", "value": root, "label": short_path(root)})
    return scopes


def in_scope(agent: Agent, scope):
    kind = (scope or ALL)["kind"]
    if kind == "session":
        return scope["value"] in (agent.session_ids or [agent.tmux_session_id])
    if kind == "project":
        return project_root(agent.cwd) == scope["value"]
    return True


def same_scope(a, b):
    return a["kind"] == b["kind"] and a["value"] == b["value"]


# ---- persistence ---------------------------------------------------------------------------

def encode_key(key):
    if key == OLDER:
        return {"more": True}
    if isinstance(key, tuple) and len(key) == 2:
        return {"agent": [key[0], key[1]]}
    return None


def decode_key(v):
    if not isinstance(v, dict):
        return None
    if v.get("more") is True:
        return OLDER
    k = v.get("agent")
    if isinstance(k, list) and len(k) == 2 and isinstance(k[0], int) and isinstance(k[1], str):
        return (k[0], k[1])
    return None


def resume_window(value):
    """@ainav-resume in minutes -> seconds; 0 = off; anything invalid = the default."""
    try:
        m = float(value)
    except (TypeError, ValueError):
        m = DEFAULT_RESUME_MIN
    if not math.isfinite(m) or m < 0:
        m = DEFAULT_RESUME_MIN
    return m * 60


def snapshot(scope, query, mode, show_older, next_key, now=None):
    return {"saved_at": now or time.time(),
            "scope": {k: scope[k] for k in ("kind", "value", "label")},
            "query": query[:MAX_QUERY], "mode": mode, "show_older": bool(show_older),
            "next": encode_key(next_key)}


def save_back(d, server, view):
    servers = d.setdefault("servers", {})
    rec = servers.setdefault(str(server), {"seen": {}, "visible": [], "created": view["saved_at"]})
    rec["back"] = view


def load_back(d, server, window_s, now=None):
    """The saved back view if it is valid and recent, else None."""
    if window_s <= 0:
        return None
    rec = d.get("servers", {}).get(str(server))
    v = rec.get("back") if isinstance(rec, dict) else None
    if not isinstance(v, dict):
        return None
    now = now or time.time()
    at = v.get("saved_at")
    if not isinstance(at, (int, float)) or not math.isfinite(at) or not 0 <= now - at <= window_s:
        return None
    s = v.get("scope")
    if not (isinstance(s, dict) and s.get("kind") in KINDS
            and all(isinstance(s.get(k), str) for k in ("value", "label"))):
        return None
    query, mode = v.get("query", ""), v.get("mode", "priority")
    if not isinstance(query, str) or mode not in MODES:
        return None
    return {"scope": {"kind": s["kind"], "value": s["value"], "label": s["label"]},
            "query": query[:MAX_QUERY], "mode": mode, "show_older": v.get("show_older") is True,
            "next": decode_key(v.get("next"))}


def worth_restoring(view):
    """Only a view you set up is worth a tab: a search, a session/project tab, OLDER expanded or
    grouping by project. Otherwise the picker just opens fresh, needs-you first."""
    return bool(view) and bool(view["query"] or view["scope"]["kind"] != "all" or view["show_older"]
                               or view["mode"] != "priority")


def next_key(keys, chosen):
    """Where the cursor should land when coming back: the row after the chosen one (or before it,
    if it was last)."""
    if chosen not in keys:
        return None
    i = keys.index(chosen)
    if i + 1 < len(keys):
        return keys[i + 1]
    return keys[i - 1] if i > 0 else None
