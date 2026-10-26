"""Discover AI agent sessions and map each one to the tmux pane it runs in."""
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import claude, codex, extras as extrasmod, state, transcript
from . import proc as procmod

DAY = 86400
CODEX_SCAN_S = 3.0
GENERIC_WINDOWS = {"", "bash", "zsh", "sh", "fish", "claude", "codex", "node", "python", "python3"}
SECTIONS = ["needs", "working", "today", "older"]
SECTION_TITLES = {"pinned": "PINNED", "needs": "NEEDS YOU", "working": "WORKING", "today": "TODAY", "older": "OLDER"}


@dataclass
class Agent:
    kind: str
    pid: int
    session_id: str
    name: str
    cwd: str
    status: str
    since: Optional[float]          # epoch seconds the status last changed
    started: Optional[float] = None
    pane_id: Optional[str] = None
    tmux_session_id: Optional[str] = None
    tmux_session: Optional[str] = None
    window_id: Optional[str] = None
    window_index: Optional[int] = None
    window_name: Optional[str] = None
    pane_title: str = ""
    other_sessions: List[str] = field(default_factory=list)  # linked windows
    session_ids: List[str] = field(default_factory=list)     # every tmux session the pane is in
    branch: str = ""
    # from the optional extras command (see extras.py)
    badge: str = ""
    badge_style: str = "dim"
    extra_title: str = ""
    extra_lines: List[str] = field(default_factory=list)
    extra_search: str = ""
    title: str = ""
    on_screen: bool = False
    done: bool = False
    later: bool = False
    pinned: bool = False
    loops: List[dict] = field(default_factory=list)  # scheduled loops still active (transcript.loops)

    @property
    def in_tmux(self):
        return self.pane_id is not None

    @property
    def location(self):
        if not self.in_tmux:
            return "(not in tmux)"
        return f"{self.tmux_session}:{self.window_index}"

    @property
    def tag(self):
        """Short project label: the tmux session name without a leading sort number ("2api" -> "api")."""
        s = self.tmux_session or ""
        return re.sub(r"^\d+[-_.]?", "", s) or s or "-"

    @property
    def window_label(self):
        """The tmux window name, unless it's a placeholder ("claude", "bash", "t-1a2b3c4d")."""
        w = self.window_name or ""
        if w.lower() in GENERIC_WINDOWS or re.match(r"^t-[0-9a-f]{6,}$", w):
            return ""
        return w

    @property
    def state_label(self):
        if self.status == "waiting":
            return "waiting"
        if self.done:
            return "done"
        if self.status == "busy":
            return "working"
        return self.status

    def age(self, now=None):
        if not self.since:
            return None
        return max(0.0, (now or time.time()) - self.since)

    def section(self, now=None):
        if self.status == "waiting" or self.done or self.later:
            return "needs"
        if self.status == "busy":
            return "working"
        # idle, and "shell" (a background shell running between turns), go by age
        age = self.age(now)
        return "today" if age is not None and age < DAY else "older"

    def search_text(self):
        return " ".join(str(x) for x in (
            self.title, self.name, self.kind, self.state_label, self.tag, self.location, self.window_name or "",
            self.cwd, self.branch, self.session_id, self.pane_id or "", " ".join(self.extra_lines),
            self.extra_search, self.badge,
            "later" if self.later else "", "pinned" if self.pinned else "",
            " ".join(["loop"] + [l["prompt"] for l in self.loops]) if self.loops else ""))


def map_to_panes(pids, panes, table, current_session=None):
    """pid -> list of Pane entries whose shell is an ancestor of pid.

    Walks each agent's parent chain against the panes' pids rather than trusting the pane
    recorded in the session file, which goes stale when panes are moved. A pane in a linked
    window appears once per session; the caller's session is listed first.
    """
    by_pid = {}
    for p in panes:
        by_pid.setdefault(p.pane_pid, []).append(p)
    out = {}
    for pid in pids:
        chain = [pid] + procmod.ancestors(pid, table)
        for a in chain:
            if a in by_pid:
                hits = sorted(by_pid[a], key=lambda p: (p.session_id != current_session, p.session_name))
                out[pid] = hits
                break
    return out


def git_branch(cwd, _cache={}):
    """Current branch (or short sha) by reading .git/HEAD; cached per cwd for a few seconds."""
    now = time.time()
    hit = _cache.get(cwd)
    if hit and now - hit[0] < 5:
        return hit[1]
    branch = ""
    d = cwd or ""
    for _ in range(40):
        if not d:
            break
        g = os.path.join(d, ".git")
        try:
            if os.path.isfile(g):  # worktree / submodule: "gitdir: <path>"
                with open(g) as f:
                    g = os.path.join(d, f.read().split("gitdir:", 1)[1].strip())
            if os.path.isdir(g):
                with open(os.path.join(g, "HEAD")) as f:
                    head = f.read().strip()
                branch = head.split("refs/heads/", 1)[1] if head.startswith("ref:") and "refs/heads/" in head else head[:8]
                break
        except (OSError, IndexError):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    _cache[cwd] = (now, branch)
    return branch


_LEAD = re.compile(r"^[^\w(\[\"']+", re.U)


def clean_title(t, hostname=None):
    """Claude's pane title minus its leading status glyph ("✳ Fix login" -> "Fix login")."""
    t = _LEAD.sub("", (t or "").strip()).strip()
    if not t or t in (hostname or socket.gethostname(), "Claude Code"):
        return ""
    return t


def weak_title(t, a):
    """Titles that identify rather than describe: "t-12faa60a" or the bare repo name.
    Names the user chose ("cogs") are kept."""
    if not t:
        return True
    return bool(re.match(r"^t-[0-9a-f]{6,}$", t)) or t == os.path.basename(a.cwd or "")


def pick_title(a, titles=None):
    """Best human label: Claude's title, the transcript's titles, the extras title, a meaningful
    window name, then the session name. Name-like titles lose to descriptive ones."""
    t = clean_title(a.pane_title)
    if t and not weak_title(t, a):
        return t
    custom, ai = titles if titles is not None else transcript.titles(a.session_id)
    for cand in (custom, ai):
        if cand and not weak_title(cand, a):
            return cand
    if a.extra_title.strip():
        return a.extra_title.strip()
    w = a.window_name or ""
    if w.lower() not in GENERIC_WINDOWS and not weak_title(w, a):
        return w
    return t or custom or a.name


def server_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def text(v):
    """A session-file field as a string; anything else (a format change) counts as missing."""
    return v if isinstance(v, str) else ""


class Discovery:
    def __init__(self, tmux, sessions_dir=None, proc=procmod.PROC, state_store=None, extras=None):
        self.tmux = tmux
        self.files = claude.SessionFiles(sessions_dir)
        self.proc = proc
        self.extras = extras or extrasmod.Extras(None)
        self.domain = procmod.pid_domain(proc)
        self.state = state_store or state.State()
        self.threads = codex.Threads()
        self._server = None
        self._codex = (0.0, [])
        transcript.load_title_cache()

    def server(self):
        if self._server is None:
            self._server = self.tmux.server_pid()
        return self._server

    def codex_tuis(self, table, now=None):
        """Codex TUI processes, re-scanned every few seconds (a scan reads the process table)."""
        now = now or time.time()
        at, found = self._codex
        if now - at >= CODEX_SCAN_S:
            try:
                found = codex.tuis(table, procmod.processes_named("codex", self.proc))
            except Exception:  # never let Codex discovery break the Claude list
                found = []
            self._codex = (now, found)
        return [(pid, argv) for pid, argv in found if table.info(pid)]

    def codex_states(self):
        """{pid: (status, process start)} of Codex TUIs in panes, from their pane titles
        (for the notification watcher: cheap, no thread lookups)."""
        table = procmod.snapshot(self.proc)
        found = self.codex_tuis(table)
        if not found:
            return {}
        panes = self.tmux.panes(strict=True)
        mapping = map_to_panes([pid for pid, _ in found], panes, table)
        out = {}
        for pid, _ in found:
            if mapping.get(pid):
                info = table.info(pid)
                out[pid] = (codex.parse_title(mapping[pid][0].pane_title)[0],
                            procmod.start_epoch(info[1], self.proc) if info else 0.0)
        return out

    def agents(self, current_session=None, include_untracked=False, record_seen=True, strict=False):
        table = procmod.snapshot(self.proc)
        entries = claude.live(self.files.read(), table, self.domain)
        found = self.codex_tuis(table)
        panes = self.tmux.panes(strict=True) if strict else self.tmux.panes()
        visible = {p.pane_id for p in panes if p.on_screen}
        if record_seen:
            st = self.update_seen(visible)
        else:
            st = self.state.read()
        view = state.server_view(st, self.server())
        mapping = map_to_panes([d["pid"] for d in entries] + [pid for pid, _ in found], panes, table,
                               current_session)
        try:
            entries += codex.entries(found, mapping, table, self.threads, proc=self.proc)
        except Exception:
            pass
        out = []
        for d in entries:
            cwd = text(d.get("cwd"))
            if d.get("agent") == "codex":
                since, started = d.get("since"), d.get("started")
            else:
                since = d.get("statusUpdatedAt") or d.get("updatedAt")
                started = d.get("startedAt")
                since = since / 1000.0 if isinstance(since, (int, float)) else None
                started = started / 1000.0 if isinstance(started, (int, float)) else None
            a = Agent(kind=d.get("agent") or "claude", pid=d["pid"], session_id=d["sessionId"],
                      name=text(d.get("name")) or os.path.basename(cwd) or d["sessionId"][:8],
                      cwd=cwd, status=text(d.get("status")) or "unknown", since=since, started=started)
            hits = mapping.get(d["pid"])
            if hits:
                p = hits[0]
                a.pane_id, a.tmux_session_id, a.tmux_session = p.pane_id, p.session_id, p.session_name
                a.window_id, a.window_index, a.window_name = p.window_id, p.window_index, p.window_name
                if a.kind == "claude":  # Codex's title is its status line, handled in codex.py
                    a.pane_title = p.pane_title
                a.other_sessions = [h.session_name for h in hits[1:]]
                a.session_ids = [h.session_id for h in hits]
                a.on_screen = a.pane_id in visible
            elif not include_untracked:
                continue
            a.branch = git_branch(a.cwd)
            a.done = state.is_done(a, view, visible)
            a.later = state.later_active(a, st)
            a.pinned = a.session_id in st["pins"]
            out.append(a)
        names = {d["sessionId"]: (text(d.get("threadName")), text(d.get("firstMessage")))
                 for d in entries if d.get("agent") == "codex"}
        info = self.extras.get(out)
        for a in out:
            extrasmod.apply(a, info.get(a.session_id) or {})
            a.title = pick_title(a, names.get(a.session_id, ("", "")) if a.kind == "codex" else None)
            if a.kind == "claude":
                a.loops = transcript.loops(a.session_id, a.started, a.status == "busy")
        transcript.save_title_cache()
        return out

    def update_seen(self, visible, extra=(), at=None, focus=None):
        """Record panes on screen now (and ones that just left the screen) as seen at `at`,
        and each client's current pane (focus: {tty: pane}) in its history."""
        st = self.state.read()
        known = st["servers"].get(str(self.server()))
        if known and set(known.get("visible", [])) == visible and not extra \
                and not state.focus_changed(st, self.server(), focus):
            return st
        with self.state.update() as st:
            state.mark_visible(st, self.server(), visible, now=at, extra_seen=extra, alive=server_alive)
            if focus:
                state.record_focus(st, self.server(), focus, now=at)
        return st

    def toggle_later(self, agent):
        with self.state.update() as st:
            return state.toggle_later(st, agent.session_id, agent.since)

    def toggle_pin(self, agent):
        with self.state.update() as st:
            return state.toggle_pin(st, agent.session_id)


def arrange(agents, mode="priority", now=None):
    """[(section_key, title, [agents])] in display order.

    priority: needs you / working / today / older, each sorted by attention then recency.
    project:  one group per tmux session, most recently active first.
    Pinned sessions come first in either mode, in their own group, most recently active first.
    """
    now = now or time.time()
    pinned = sorted((a for a in agents if a.pinned), key=lambda a: a.age(now) if a.since else float("inf"))
    head = [("pinned", SECTION_TITLES["pinned"], pinned)] if pinned else []
    agents = [a for a in agents if not a.pinned]

    def rank(a):
        attention = 0 if a.status == "waiting" else 1 if a.done else 2 if a.later else 3
        return (attention, a.age(now) if a.since else float("inf"))

    if mode == "project":
        groups = {}
        for a in agents:
            groups.setdefault(a.tag, []).append(a)
        ordered = sorted(groups.items(), key=lambda kv: min(rank(a) for a in kv[1]))
        return head + [(f"p:{tag}", tag.upper(), sorted(items, key=rank)) for tag, items in ordered]
    by = {k: [] for k in SECTIONS}
    for a in agents:
        by[a.section(now)].append(a)
    return head + [(k, SECTION_TITLES[k], sorted(by[k], key=rank)) for k in SECTIONS if by[k]]


def sort_agents(agents, mode="priority", now=None):
    """Flat order (for `list`): the sections concatenated."""
    if mode == "session":
        return sorted(agents, key=lambda a: (a.tmux_session or "~", a.window_index or 0, a.name))
    return [a for _, _, items in arrange(agents, "priority", now) for a in items]


def fmt_age(sec):
    if sec is None:
        return "-"
    m = int(sec // 60)
    if m < 1:
        return "<1m"
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m" if h < 10 else f"{h}h"
    return f"{h // 24}d"
