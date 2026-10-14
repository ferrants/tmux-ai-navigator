"""Codex CLI sessions, found from the panes they run in.

Codex runs the conversations of every TUI in one shared app-server process, so neither its
hooks nor its process tree say which pane a conversation belongs to. The TUI itself runs in
the pane, though, and keeps the pane title current:

  "⠼ <name> | <project>"                        working (a braille spinner)
  "[ ! ] Action Required | <name> | <project>"  waiting for an approval ("[ . ]" as it blinks)
  "<name> | <project>", " <project>"            idle (older versions show only the project)

So the status comes from the title, and the conversation (name, timing, preview) is matched
from Codex's thread database: by the name in the title, `codex resume <id>`, or the folder and
start time. Like Claude's files, these are undocumented internals: every field is optional.
"""
import calendar
import glob
import json
import os
import re
import sqlite3
import time

from . import proc as procmod

# subcommands that aren't the interactive TUI
NOT_TUI = {"exec", "e", "review", "app-server", "mcp", "mcp-server", "login", "logout", "completion",
           "sandbox", "debug", "apply", "a", "cloud", "features", "help", "proto", "generate-ts",
           "responses-api-proxy", "stdio-to-uds", "execpolicy", "update", "--version", "-V", "--help", "-h"}
VALUE_FLAGS = {"-m", "--model", "-c", "--config", "-C", "--cd", "-s", "--sandbox", "-a", "--ask-for-approval",
               "-p", "--profile", "-i", "--image", "--enable", "--disable", "--add-dir", "--local-provider"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
WAITING = re.compile(r"^\[ [!.] \]\s*")
TAIL_BYTES = 256 * 1024
THREADS_TTL = 3.0

_rollouts = {}  # session id -> rollout path, for the preview
_tails = {}     # path -> ((size, mtime), parsed tail)


def home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


# ---- the TUI processes and their panes -----------------------------------------------------

def positional(argv):
    """The positional arguments (subcommand, prompt, ids), skipping flags and their values.
    A flag that ends the TUI ("--help") is returned as if it were a subcommand."""
    out, skip = [], False
    for tok in argv[1:]:
        if skip:
            skip = False
        elif tok == "--":
            break
        elif tok.startswith("-"):
            if tok in NOT_TUI:
                out.append(tok)
            skip = tok in VALUE_FLAGS
        else:
            out.append(tok)
    return out


def first_arg(argv):
    args = positional(argv)
    return args[0] if args else ""


def is_tui(argv):
    if not argv or os.path.basename(argv[0]) != "codex":
        return False
    return first_arg(argv) not in NOT_TUI


def resume_id(argv):
    """The conversation id from `codex resume <id>`, if given. (`codex fork <id>` names the
    source conversation; the fork itself gets a new id.)"""
    args = positional(argv)
    if len(args) >= 2 and args[0] == "resume" and UUID.match(args[1]):
        return args[1]
    return ""


def tuis(table, procs=None):
    """[(pid, argv)] of Codex TUIs, outermost only (a TUI started from inside another is the
    same pane's inner program)."""
    found = [(pid, argv) for pid, argv in (procs if procs is not None else procmod.processes_named("codex"))
             if is_tui(argv)]
    pids = {pid for pid, _ in found}
    return [(pid, argv) for pid, argv in found if not pids & set(procmod.ancestors(pid, table))]


def parse_title(title):
    """(status, conversation name) from a Codex TUI's pane title."""
    t = (title or "").strip()
    status = "idle"
    m = WAITING.match(t)
    if m:
        status = "waiting"
        t = t[m.end():]
        parts = [p.strip() for p in t.split(" | ")]
        if parts and parts[0].lower() == "action required":
            parts = parts[1:]
    else:
        if t and 0x2800 <= ord(t[0]) <= 0x28FF:
            status = "busy"
            t = t[1:].strip()
        parts = [p.strip() for p in t.split(" | ")]
    parts = [p for p in parts if p]
    return status, (parts[0] if len(parts) >= 2 else "")


# ---- threads ---------------------------------------------------------------------------------

def state_db(base=None):
    """The newest state_<n>.sqlite."""
    best, best_n = None, -1
    for path in glob.glob(os.path.join(base or home(), "state_*.sqlite")):
        m = re.search(r"state_(\d+)\.sqlite$", path)
        if m and int(m.group(1)) > best_n:
            best, best_n = path, int(m.group(1))
    return best


def interactive(row):
    source, origin = row.get("source") or "", row.get("originator") or ""
    return not (origin == "codex_exec" or row.get("thread_source") == "subagent"
                or source == "exec" or source.startswith("{"))


class Threads:
    """Codex's thread table (read-only), re-read at most every few seconds."""

    def __init__(self, path=None):
        self.path = path
        self._at = 0.0
        self._rows = []

    def rows(self, now=None):
        now = now or time.time()
        if now - self._at < THREADS_TTL:
            return self._rows
        self._at = now
        path = self.path or state_db()
        if not path:
            self._rows = []
            return self._rows
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
            try:
                cols = {r[1] for r in con.execute("pragma table_info(threads)")}
                want = [c for c in ("id", "rollout_path", "cwd", "name", "title", "first_user_message",
                                    "created_at", "created_at_ms", "updated_at", "updated_at_ms", "source",
                                    "thread_source", "originator", "archived") if c in cols]
                if "id" not in want:
                    raise sqlite3.DatabaseError("no thread ids")
                order = "updated_at_ms" if "updated_at_ms" in cols else "updated_at" if "updated_at" in cols else "rowid"
                cur = con.execute(f"select {', '.join(want)} from threads order by {order} desc limit 2000")
                rows = [dict(zip(want, r)) for r in cur]
            finally:
                con.close()
        except sqlite3.Error:
            return self._rows  # busy or changed: keep what we had
        out = []
        for r in rows:
            if r.get("archived") or not interactive(r):
                continue
            r["created"] = ms_or_s(r.get("created_at_ms"), r.get("created_at"))
            r["updated"] = ms_or_s(r.get("updated_at_ms"), r.get("updated_at"))
            out.append(r)
        self._rows = out
        return out


def ms_or_s(ms, s):
    if isinstance(ms, (int, float)) and ms:
        return ms / 1000.0
    return float(s) if isinstance(s, (int, float)) else 0.0


def match(sessions, threads):
    """{pid: thread row} for TUIs [{pid, start, cwd, name, resume}], each thread used once.

    A conversation name in the title is the best evidence, then an explicit resume id, then
    the latest conversation in the TUI's folder that was active since the TUI started.
    """
    out, used = {}, set()
    by_id = {t["id"]: t for t in threads}

    def take(s, t):
        out[s["pid"]] = t
        used.add(t["id"])

    for s in sessions:
        if s.get("name"):
            cands = [t for t in threads if t["id"] not in used and (t.get("name") or "") == s["name"]
                     and (t.get("cwd") or "") == s["cwd"]]
            if cands:
                take(s, max(cands, key=lambda t: t["updated"]))
    for s in sessions:
        if s["pid"] not in out and s.get("resume") in by_id and s["resume"] not in used:
            take(s, by_id[s["resume"]])
    # newest TUIs pick first: an older TUI in the same folder is the one that went quiet
    for s in sorted(sessions, key=lambda s: -(s.get("start") or 0)):
        if s["pid"] in out:
            continue
        cands = [t for t in threads if t["id"] not in used and (t.get("cwd") or "") == s["cwd"]
                 and t["updated"] >= (s.get("start") or 0) - 5]
        if cands:
            take(s, max(cands, key=lambda t: t["updated"]))
    return out


# ---- the conversation log ---------------------------------------------------------------------

def _ts(v):
    """ISO 8601 ("2026-10-14T16:59:57.792Z") -> epoch seconds, or 0."""
    if not isinstance(v, str):
        return 0.0
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?Z?$", v)
    if not m:
        return 0.0
    secs = calendar.timegm(tuple(int(x) for x in m.groups()[:6]) + (0, 0, 0))
    return secs + float(m.group(7) or 0)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"))
    return ""


def tail(path):
    """{"started", "finished", "last", "user", "assistant"} from the end of a rollout file:
    when the last turn started and ended, the latest event, and the last prompt and reply."""
    try:
        st = os.stat(path)
    except (OSError, TypeError):
        return {}
    key = (st.st_size, st.st_mtime_ns)
    hit = _tails.get(path)
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
    out = {"started": 0.0, "finished": 0.0, "last": st.st_mtime, "user": "", "assistant": ""}
    for line in reversed(lines):
        if out["started"] and out["finished"] and out["user"] and out["assistant"]:
            break
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        kind = p.get("type")
        if d.get("type") == "event_msg":
            if kind == "task_started" and not out["started"]:
                out["started"] = _ts(d.get("timestamp"))
            elif kind in ("task_complete", "turn_aborted") and not out["finished"]:
                out["finished"] = _ts(d.get("timestamp"))
        elif d.get("type") == "response_item" and kind == "message":
            text = _text(p.get("content")).strip()
            if p.get("role") == "assistant" and not out["assistant"] and text:
                out["assistant"] = text
            elif p.get("role") == "user" and not out["user"] and text and not text.startswith("<"):
                out["user"] = text
    _tails[path] = (key, out)
    return out


def summary(session_id):
    path = _rollouts.get(session_id)
    if not path:
        return {}
    t = tail(path)
    return {"user": t.get("user", ""), "assistant": t.get("assistant", "")} if t else {}


def knows(session_id):
    return session_id in _rollouts


# ---- putting it together ----------------------------------------------------------------------

def entries(found, mapping, table, threads, now=None, proc=procmod.PROC):
    """Session records, in the shape Claude's session files have, for TUIs that are in a pane.

    found: [(pid, argv)] from tuis(); mapping: pid -> [Pane] from map_to_panes().
    """
    now = now or time.time()
    sessions = []
    for pid, argv in found:
        hits = mapping.get(pid)
        if not hits:
            continue
        status, name = parse_title(hits[0].pane_title)
        info = table.info(pid)
        sessions.append({"pid": pid, "start": procmod.start_epoch(info[1], proc) if info else 0.0,
                         "procStart": info[1] if info else None, "cwd": hits[0].pane_current_path,
                         "name": name, "resume": resume_id(argv), "status": status})
    matched = match(sessions, threads.rows(now) if threads else [])
    out = []
    for s in sessions:
        t = matched.get(s["pid"])
        rec = {"agent": "codex", "pid": s["pid"], "procStart": s["procStart"], "status": s["status"],
               "cwd": s["cwd"], "sessionId": f"codex-{s['pid']}", "started": s["start"] or None}
        since = s["start"] or None
        if t:
            rec["sessionId"] = t["id"]
            rec["cwd"] = t.get("cwd") or s["cwd"]
            rec["threadName"] = t.get("name") or ""
            first = t.get("first_user_message") or t.get("title") or ""
            rec["firstMessage"] = first.strip().splitlines()[0][:80] if first.strip() else ""
            rec["started"] = t["created"] or rec["started"]
            path = t.get("rollout_path")
            if path:
                _rollouts[t["id"]] = path
                tl = tail(path)
                if s["status"] == "busy":
                    since = tl.get("started") or tl.get("last")
                elif s["status"] == "waiting":
                    since = tl.get("last")
                else:
                    since = tl.get("finished") or t["updated"] or since
        rec["since"] = since
        out.append(rec)
    return out
