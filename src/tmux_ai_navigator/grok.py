"""grok (the xAI "Grok Build" TUI) sessions, found from the panes they run in.

Each grok TUI is one process, and grok keeps what we need on disk under $GROK_HOME (~/.grok):

  active_sessions.json   [{session_id, pid, pid_start, cwd, opened_at}] for running TUIs, where
                         pid_start is the same start token proc.py reads (so a reused pid is
                         told apart)
  sessions/<cwd>/<id>/   events.jsonl (turns, tool permissions), summary.json (generated
                         title, times), chat_history.jsonl (the conversation)

The status comes from events.jsonl: a turn that has started and not ended is "busy", and a tool
permission request still unanswered after GRACE_S is "waiting". (grok logs a request and its
answer for every tool call, including ones it allows on its own, within a millisecond.) Like
Claude's and Codex's files, these are undocumented internals: every field is optional.
"""
import glob
import json
import os
import re
import time
from urllib.parse import quote

from . import proc as procmod
from .codex import _ts

# subcommands that aren't the interactive TUI ("dashboard" is one)
NOT_TUI = {"agent", "clone", "completions", "doctor", "du", "disk-usage", "export", "help", "inspect", "leader",
           "login", "logout", "mcp", "memory", "models", "plugin", "sessions", "setup", "trace", "update", "usage",
           "version", "v", "worker", "worktree", "wrap"}
HEADLESS = {"-p", "--single", "--prompt-file", "--prompt-json", "-h", "--help", "-v", "--version"}
VALUE_FLAGS = {"--add-dir", "--agent", "--agents", "--allow", "--allowedTools", "--allowed-tools", "--cwd",
               "--debug-file", "--deny", "--disallowedTools", "--disallowed-tools", "--json-schema",
               "--leader-socket", "-m", "--model", "--output-format", "--permission-mode", "--reasoning-effort",
               "--effort", "--rules", "-s", "--session-id", "--sandbox", "--system-prompt-override",
               "--system-prompt", "--tools", "--worktree-ref", "--ref"}
OPTIONAL_VALUE = {"-r", "--resume", "-w", "--worktree"}
NAME = re.compile(r"^grok(-\d[\w.+-]*)?$")  # "grok", or the versioned binary ("grok-1.0.77-lin" in comm)
QUERY = re.compile(r"<user_query>\s*(.*?)\s*(?:</user_query>|$)", re.S)
GRACE_S = 1.5         # an unanswered permission request counts as waiting after this long
COLD_BYTES = 512 * 1024  # a first read of events.jsonl starts this far from the end
TAIL_BYTES = 256 * 1024

_dirs = {}      # session id -> session dir, for the preview
_logs = {}      # events.jsonl path -> EventLog
_summaries = {}  # summary.json path -> ((size, mtime), dict)
_chats = {}     # chat_history.jsonl path -> ((size, mtime), preview)


def home():
    return os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")


def is_name(name):
    return bool(NAME.match(name or ""))


# ---- the TUI processes ---------------------------------------------------------------------

def is_tui(argv):
    """True for the interactive TUI: not a subcommand ("grok sessions") or a headless run
    ("grok -p ..."). A positional argument that isn't a subcommand is the first prompt."""
    if not argv or not is_name(os.path.basename(argv[0])):
        return False
    first, skip, rest, i = None, False, False, 1
    while i < len(argv):
        tok = argv[i]
        i += 1
        if skip:
            skip = False
            continue
        if rest or not tok.startswith("-") or tok == "-":
            if first is None:
                first = (tok, rest)
            continue
        if tok == "--":
            rest = True
            continue
        if tok.startswith("--"):
            name, has_value = tok.split("=", 1)[0], "=" in tok
        else:
            name, has_value = tok[:2], len(tok) > 2
        if name in HEADLESS:
            return False
        if has_value:
            continue
        if name in VALUE_FLAGS:
            skip = True
        elif name in OPTIONAL_VALUE and i < len(argv) and not argv[i].startswith("-"):
            skip = True
    return not (first and not first[1] and first[0] in NOT_TUI)


def tuis(table, procs):
    """[(pid, argv)] of grok TUIs, outermost only (a grok started from inside another grok's
    shell belongs to the same pane)."""
    found = [(pid, argv) for pid, argv in procs if is_tui(argv)]
    pids = {pid for pid, _ in found}
    return [(pid, argv) for pid, argv in found if not pids & set(procmod.ancestors(pid, table))]


# ---- which session a process is showing -------------------------------------------------------

class ActiveSessions:
    """$GROK_HOME/active_sessions.json, re-read when it changes. If a read fails, the last good
    copy stands."""

    def __init__(self, path=None):
        self.path = path
        self._key = None
        self._rows = []

    def rows(self):
        path = self.path or os.path.join(home(), "active_sessions.json")
        try:
            st = os.stat(path)
        except OSError:
            self._key, self._rows = None, []
            return self._rows
        key = (path, st.st_size, st.st_mtime_ns)
        if key == self._key:
            return self._rows
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return self._rows
        self._key = key
        self._rows = [r for r in data if isinstance(r, dict) and isinstance(r.get("session_id"), str)
                      and isinstance(r.get("pid"), int)] if isinstance(data, list) else []
        return self._rows

    def lookup(self, pid, token, start):
        """(session id, cwd) for a running process, or None.

        The entry's pid_start must equal the process's start token. Where tokens aren't
        comparable (macOS), the entry has to have been opened after the process started.
        """
        token = str(token or "")
        cands = []
        for r in self.rows():
            if r["pid"] != pid:
                continue
            if token.isdigit():
                if str(r.get("pid_start")) != token:
                    continue
            elif not (start and _ts(r.get("opened_at")) >= start - 5):
                continue
            cands.append(r)
        if not cands:
            return None
        r = max(cands, key=lambda r: _ts(r.get("opened_at")))
        return r["session_id"], r.get("cwd") if isinstance(r.get("cwd"), str) else ""


def open_session(pid, proc=procmod.PROC, base=None):
    """The session whose events.jsonl the process has open (Linux), if exactly one."""
    root = os.path.join(base or home(), "sessions") + os.sep
    found = set()
    try:
        fds = os.listdir(os.path.join(proc, str(pid), "fd"))
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(os.path.join(proc, str(pid), "fd", fd))
        except OSError:
            continue
        if target.startswith(root) and target.endswith(os.sep + "events.jsonl"):
            found.add(os.path.basename(os.path.dirname(target)))
    return found.pop() if len(found) == 1 else None


def session_dir(session_id, cwd="", base=None):
    """$GROK_HOME/sessions/<url-encoded cwd>/<id>, or None if it isn't there (yet)."""
    base = base or home()
    key = (base, session_id)
    hit = _dirs.get(key)
    if hit and os.path.isdir(hit):
        return hit
    path = os.path.join(base, "sessions", quote(cwd, safe=""), session_id) if cwd else ""
    if not (path and os.path.isdir(path)):
        hits = [p for p in glob.glob(os.path.join(glob.escape(base), "sessions", "*", glob.escape(session_id)))
                if os.path.isdir(p)]
        path = hits[0] if len(hits) == 1 else ""
    if path:
        _dirs[key] = path
        _dirs[session_id] = path
    return path or None


# ---- status from events.jsonl ------------------------------------------------------------------

class EventLog:
    """The turn and permission state of one events.jsonl, read incrementally.

    A first read starts COLD_BYTES from the end (only the latest turn matters) and falls back to
    the whole file if that stretch has no turn boundary in it. After that only appended lines
    are read; a line still being written waits for the next read. If the file is replaced or
    shrinks, it's read again from the start.
    """

    def __init__(self, path):
        self.path = path
        self.ident = None
        self.offset = 0
        self.ok = False  # read successfully at least once
        self._reset()

    def _reset(self):
        self.turn = False
        self.turn_ts = 0.0
        self.ended_ts = 0.0
        self.resumed_ts = 0.0  # when a real permission wait ended: busy again from then
        self.pending = []      # [(tool name, requested at)]
        self.nested = 0        # subagent turns running inside the main one
        self.boundary = False

    def read(self):
        """Bring the state up to date. False if the file couldn't be read (the state stands)."""
        try:
            st = os.stat(self.path)
            ident = (st.st_dev, st.st_ino)
            if ident != self.ident or st.st_size < self.offset:
                self._reset()
                self.ident, self.offset = ident, 0
                start = max(0, st.st_size - COLD_BYTES)
                if not self._consume(start, st.st_size) and start:
                    self._reset()
                    self._consume(0, st.st_size)
            elif st.st_size > self.offset:
                self._consume(self.offset, st.st_size)
        except OSError:
            return False
        self.ok = True
        return True

    def _consume(self, start, end):
        """Apply the complete lines in [start, end). True if a turn boundary was seen."""
        self.boundary = False
        with open(self.path, "rb") as f:
            f.seek(start)
            data = f.read(end - start)
        cut = data.rfind(b"\n")
        if cut < 0:
            return False  # no complete line yet: read it all again next time
        lines = data[:cut + 1].split(b"\n")
        if start and start != self.offset:
            lines = lines[1:]  # began mid-line
        self.offset = start + cut + 1
        for line in lines:
            if b'"turn_' in line or b'"permission_' in line:
                self._apply(line)
        return self.boundary

    def _apply(self, line):
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        kind, ts = d.get("type"), _ts(d.get("ts"))
        if kind == "turn_started":
            if d.get("session_relationship", "primary") != "primary":
                self.nested += 1  # its turn_ended doesn't say whose it is: skip that one too
                return
            self.turn, self.turn_ts, self.resumed_ts, self.pending, self.boundary = True, ts, 0.0, [], True
            self.nested = 0
        elif kind == "turn_ended":
            if self.nested:
                self.nested -= 1
                return
            self.turn, self.ended_ts, self.pending, self.boundary = False, ts, [], True
        elif kind == "permission_requested":
            self.pending.append((d.get("tool_name"), ts))
        elif kind == "permission_resolved":
            for i, (tool, at) in enumerate(self.pending):
                if tool == d.get("tool_name"):
                    del self.pending[i]
                    if ts - at >= GRACE_S:
                        self.resumed_ts = ts
                    break

    def status(self, now=None):
        """(status, since): waiting, busy or idle, and when that status began (0 if unknown)."""
        now = now or time.time()
        if self.pending:
            at = min(at for _, at in self.pending)
            if now - at >= GRACE_S:
                return "waiting", at
        if self.turn:
            return "busy", max(self.turn_ts, self.resumed_ts)
        if self.pending:  # a request outside a turn: grok is asking, just not for long yet
            return "busy", self.resumed_ts or min(at for _, at in self.pending)
        return "idle", self.ended_ts


def event_log(path):
    log = _logs.get(path)
    if log is None:
        log = _logs[path] = EventLog(path)
    log.read()
    return log


def read_summary(path):
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_size, st.st_mtime_ns)
    hit = _summaries.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return hit[1] if hit else {}
    data = data if isinstance(data, dict) else {}
    _summaries[path] = (key, data)
    return data


def text(v):
    return v.strip() if isinstance(v, str) else ""


# ---- the preview ---------------------------------------------------------------------------------

def _user_text(d):
    if "prompt_index" not in d or d.get("synthetic_reason"):
        return ""
    c = d.get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
    if not isinstance(c, str):
        return ""
    m = QUERY.search(c)
    return (m.group(1) if m else c).strip()


def chat_tail(path):
    """{"user", "assistant"}: the last prompt the user typed and the last reply with text."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_size, st.st_mtime_ns)
    hit = _chats.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return {}
    lines = data.splitlines()
    if st.st_size > TAIL_BYTES:
        lines = lines[1:]
    out = {"user": "", "assistant": ""}
    for line in reversed(lines):
        if out["user"] and out["assistant"]:
            break
        if '"user"' not in line and '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "assistant" and not out["assistant"]:
            out["assistant"] = text(d.get("content"))
        elif d.get("type") == "user" and not out["user"]:
            out["user"] = _user_text(d)
    _chats[path] = (key, out)
    return out


def summary(session_id):
    d = _dirs.get(session_id)
    if not d:
        return {}
    return chat_tail(os.path.join(d, "chat_history.jsonl"))


def knows(session_id):
    return session_id in _dirs


# ---- putting it together ---------------------------------------------------------------------------

def state(pid, table, registry, now=None, proc=procmod.PROC):
    """{"sessionId", "cwd", "status", "since", "started", "procStart", "dir"} for a grok process
    (no tmux needed: the notification watcher calls this). The status is "unknown" if the
    session's events couldn't be read."""
    now = now or time.time()
    info = table.info(pid)
    token = info[1] if info else None
    start = procmod.start_epoch(token, proc) if token else 0.0
    out = {"sessionId": f"grok-{pid}", "cwd": "", "status": "idle", "since": start or None,
           "started": start or None, "procStart": token, "dir": None}
    found = registry.lookup(pid, token, start)
    if not found:
        sid = open_session(pid, proc)
        found = (sid, "") if sid else None
    if not found:
        return out  # not prompted yet, or grok changed its files: listed, as idle
    sid, cwd = found
    out["sessionId"], out["cwd"] = sid, cwd
    d = session_dir(sid, cwd)
    if not d:
        return out  # a brand-new session: grok creates its folder with the first prompt
    out["dir"] = d
    summ = read_summary(os.path.join(d, "summary.json"))
    out["started"] = _ts(summ.get("created_at")) or out["started"]
    out["title"] = text(summ.get("generated_title")) or text(summ.get("session_summary"))
    log = event_log(os.path.join(d, "events.jsonl"))
    if not log.ok:
        out["status"] = "unknown"
        return out
    status, since = log.status(now)
    out["status"] = status
    out["since"] = since or (_ts(summ.get("last_active_at")) if status == "idle" else 0.0) or out["since"]
    return out


def pane_name(title):
    """The conversation title grok puts in the pane title ("<title> - grok"), unless cut short."""
    t = (title or "").strip()
    if not t.endswith(" - grok"):
        return ""
    t = t[:-len(" - grok")].strip()
    return "" if t.endswith("…") else t


def entries(found, mapping, table, registry, now=None, proc=procmod.PROC):
    """Session records, in the shape Claude's session files have, for TUIs that are in a pane.

    found: [(pid, argv)] from tuis(); mapping: pid -> [Pane] from map_to_panes().
    """
    out = []
    for pid, _ in found:
        hits = mapping.get(pid)
        if not hits:
            continue
        s = state(pid, table, registry, now, proc)
        if s["dir"]:
            _dirs[s["sessionId"]] = s["dir"]
        out.append({"agent": "grok", "pid": pid, "procStart": s["procStart"], "sessionId": s["sessionId"],
                    "cwd": s["cwd"] or hits[0].pane_current_path, "status": s["status"], "since": s["since"],
                    "started": s["started"], "threadName": s.get("title", ""),
                    "firstMessage": pane_name(hits[0].pane_title)})
    return out
