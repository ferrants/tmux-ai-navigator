"""Last user prompt, last assistant reply, title and scheduled loops from a Claude transcript
(.jsonl).

The preview reads only the tail of the file, cached by (path, size, mtime), so it is cheap
enough to call for the selected row on every redraw. Titles and loops can sit anywhere in the
file, so it is scanned once and then only new bytes are read; the scan position and what it
found are kept between runs.

Loops: Claude keeps CronCreate jobs and /loop wakeups in memory only ("session-only, dies when
Claude exits"), so they are replayed from the tool calls and results in the transcript:
CronCreate adds a job, CronDelete removes one, CronList says exactly which exist and
ScheduleWakeup sets or stops the next wakeup. loops() then drops what has ended: jobs from
before the current process started, recurring jobs past their expiry, one-shots that fired.
"""
import glob
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

from . import claude, codex

TAIL_BYTES = 512 * 1024
_paths = {}
_cache = {}
_scans = {}  # path -> [bytes scanned, custom title, ai title, schedule]; persisted between runs
_scans_dirty = False
SCHED_TOOLS = ("CronCreate", "CronDelete", "CronList", "ScheduleWakeup")
_TOOL_USE = re.compile(rb'"name": ?"(?:CronCreate|CronDelete|CronList|ScheduleWakeup)"')
DEFAULT_EXPIRY_DAYS = 7
WAKEUP_GRACE_S = 15 * 60  # a /loop turn still running when its wakeup is due reschedules itself
MAX_PENDING = 50
SCAN_CHUNK = 8 * 1024 * 1024


def _cache_file():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "tmux-ai-navigator", "scan.json")


def load_title_cache():
    try:
        with open(_cache_file()) as f:
            data = json.load(f)
        for path, v in data.items():
            if isinstance(v, list) and len(v) == 4 and isinstance(v[0], int) and isinstance(v[3], dict) \
                    and path not in _scans:
                _scans[path] = v
    except (OSError, ValueError, AttributeError):
        pass


def save_title_cache():
    """Write the scan positions back (only live files, only if something changed)."""
    global _scans_dirty
    if not _scans_dirty:
        return
    path = _cache_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({p: v for p, v in _scans.items() if os.path.exists(p)}, f)
        os.replace(tmp, path)
        _scans_dirty = False
        old = os.path.join(os.path.dirname(path), "titles.json")  # before loops were tracked
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
_TAG = re.compile(r"<(system-reminder|local-command-[a-z]+|command-[a-z]+)>.*?</\1>", re.S)


def find(session_id):
    p = _paths.get(session_id)
    if p and os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(claude.config_dir(), "projects", "*", f"{session_id}.jsonl"))
    p = max(hits, key=os.path.getmtime) if hits else None
    _paths[session_id] = p
    return p


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _clean_user(text):
    text = _TAG.sub("", text).strip()
    if not text or text.startswith(("Caveat:", "[Request interrupted")):
        return ""
    return text


def _when(ts):
    """Transcript timestamp ("2026-10-26T15:39:56.086Z") -> epoch seconds, or None."""
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _short(prompt):
    first = next((l.strip() for l in str(prompt or "").splitlines() if l.strip()), "")
    return first[:120]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _schedule_event(sched, d):
    """Apply one transcript entry's scheduling tool calls / results to sched."""
    pending = sched.setdefault("pending", {})
    jobs = sched.setdefault("jobs", {})
    content = (d.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_use" and c.get("name") in SCHED_TOOLS and isinstance(c.get("id"), str):
            inp = c.get("input") if isinstance(c.get("input"), dict) else {}
            pending[c["id"]] = [c["name"], {k: inp[k] for k in ("cron", "prompt", "recurring", "id", "jobId", "stop")
                                            if k in inp}]
            while len(pending) > MAX_PENDING:
                pending.pop(next(iter(pending)))
        elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
            name, inp = pending.pop(c["tool_use_id"])
            r = d.get("toolUseResult")
            r = r if isinstance(r, dict) else {}
            at = _when(d.get("timestamp")) or time.time()
            text = _text(c.get("content")) if not isinstance(c.get("content"), str) else c["content"]
            if c.get("is_error"):
                continue
            if name == "CronCreate" and isinstance(r.get("id"), str):
                m = re.search(r"Auto-expires after (\d+) days?", text or "")
                nxt = _num(r.get("nextFireAt"))
                jobs[r["id"]] = {"at": at, "recurring": r.get("recurring", inp.get("recurring", True)) is not False,
                                 "durable": r.get("durable") is True, "cron": str(inp.get("cron") or ""),
                                 "human": str(r.get("humanSchedule") or inp.get("cron") or ""),
                                 "prompt": _short(inp.get("prompt")), "next": nxt / 1000 if nxt else None,
                                 "days": int(m.group(1)) if m else DEFAULT_EXPIRY_DAYS}
            elif name == "CronDelete":
                jid = r.get("id") if isinstance(r.get("id"), str) else inp.get("id") or inp.get("jobId")
                if jid and (r.get("id") or (text or "").startswith("Cancelled")):
                    jobs.pop(jid, None)
            elif name == "CronList" and isinstance(r.get("jobs"), list):
                listed = {}
                for j in r["jobs"]:
                    if not isinstance(j, dict) or not isinstance(j.get("id"), str):
                        continue
                    old = jobs.get(j["id"]) or {}
                    listed[j["id"]] = dict(old, **{
                        "at": old.get("at", at), "recurring": j.get("recurring", old.get("recurring", True)) is not False,
                        "durable": j.get("durable", old.get("durable", False)) is True,
                        "cron": str(j.get("cron") or old.get("cron") or ""),
                        "human": str(j.get("humanSchedule") or old.get("human") or j.get("cron") or ""),
                        "prompt": _short(j.get("prompt")) or old.get("prompt", ""),
                        "next": old.get("next"), "days": old.get("days", DEFAULT_EXPIRY_DAYS)})
                sched["jobs"] = listed
            elif name == "ScheduleWakeup":
                when = _num(r.get("scheduledFor"))
                if r.get("stopped") or inp.get("stop"):
                    sched["wakeup"] = None
                elif when:
                    sched["wakeup"] = {"at": when / 1000, "set": at, "prompt": _short(inp.get("prompt"))}


def _scan_lines(rec, data):
    sched = rec[3]
    for line in data.split(b"\n"):
        titled = b'Title"' in line
        scheduling = _TOOL_USE.search(line) or (sched.get("pending") and b'"tool_result"' in line
                                                and any(k.encode() in line for k in sched["pending"]))
        if not (titled or scheduling):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "custom-title" and d.get("customTitle"):
            rec[1] = d["customTitle"]
        elif d.get("type") == "ai-title" and d.get("aiTitle"):
            rec[2] = d["aiTitle"]
        elif scheduling:
            _schedule_event(sched, d)


def _scan(session_id):
    """[bytes scanned, custom title, ai title, schedule] for the session's transcript, or None."""
    path = find(session_id)
    if not path:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    rec = _scans.get(path)
    if not rec or size < rec[0]:
        rec = [0, "", "", {}]
    if size > rec[0]:
        try:
            with open(path, "rb") as f:
                f.seek(rec[0])
                while rec[0] < size:  # in chunks: a first scan can cover hundreds of MB
                    data = f.read(min(SCAN_CHUNK, size - rec[0]))
                    end = data.rfind(b"\n") + 1  # leave a partial last line for the next read
                    if not end:
                        if len(data) < SCAN_CHUNK:
                            break
                        data += f.readline()  # one line longer than a chunk
                        end = data.rfind(b"\n") + 1
                        if not end:
                            break
                    _scan_lines(rec, data[:end])
                    rec[0] += end
                    f.seek(rec[0])
        except OSError:
            pass
        _scans[path] = rec
        global _scans_dirty
        _scans_dirty = True
    return rec


def titles(session_id):
    """(custom title, ai title), the latest of each anywhere in the transcript."""
    rec = _scan(session_id)
    return (rec[1], rec[2]) if rec else ("", "")


def _cron_field(spec, lo, hi):
    """Values a cron field allows ("*", "*/5", "1-5", "1,15", "10-50/10"), or None if unreadable."""
    out = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        if not (step.isdigit() or not step) or (step and int(step) == 0):
            return None
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            a, b = int(a), int(b)
        elif rng.isdigit():
            a = b = int(rng)
            if step:
                b = hi
        else:
            return None
        if a < lo or b > hi or a > b:
            return None
        out.update(range(a, b + 1, int(step or 1)))
    return out


_once_cache = {}


def _once_at(cron, after):
    """When a one-shot cron (local time) first fires at or after `after`; None if unclear."""
    key = (cron, after)
    if key in _once_cache:
        return _once_cache[key]
    t = None
    f = cron.split()
    if len(f) == 5 and all(x.isdigit() for x in f[:4]) and f[4] == "*":  # "M H D Mon *": the usual one-shot
        minute, hour, day, month = (int(x) for x in f[:4])
        year = time.localtime(after).tm_year
        for y in (year, year + 1):
            try:
                c = time.mktime((y, month, day, hour, minute, 0, 0, 0, -1))
            except (OverflowError, ValueError):
                break
            lt = time.localtime(c)
            if (lt.tm_mon, lt.tm_mday) != (month, day):  # mktime rolls Feb 31 into March
                continue
            if c >= after:
                t = c
                break
    elif len(f) == 5:
        fields = [_cron_field(x, lo, hi) for x, (lo, hi) in zip(f, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)))]
        if None not in fields:
            minutes, hours, days, months, dows = fields
            dows = {d % 7 for d in dows}
            dom_any, dow_any = f[2] == "*", f[4] == "*"
            start = (int(after) // 60 + 1) * 60  # crons fire at the start of a minute after creation
            day0 = date.fromtimestamp(start)
            for n in range(367):  # day by day for a year, then the first matching time that day
                d = day0 + timedelta(days=n)
                dow = d.isoweekday() % 7  # cron: 0 = Sunday
                day_ok = (d.day in days or dow in dows) if not (dom_any or dow_any) else \
                    (d.day in days and dow in dows)
                if d.month not in months or not day_ok:
                    continue
                c = next((c for c in (time.mktime((d.year, d.month, d.day, h, m, 0, 0, 0, -1))
                                      for h in sorted(hours) for m in sorted(minutes)) if c >= start), None)
                if c is not None:
                    t = c
                    break
    if len(_once_cache) > 500:
        _once_cache.clear()
    _once_cache[key] = t
    return t


def loops(session_id, started=None, busy=False, now=None):
    """Scheduled loops still active in a Claude session:
    [{"kind": "cron" | "once" | "wakeup", "schedule", "prompt", "next" (epoch or None)}]."""
    rec = _scan(session_id)
    if not rec:
        return []
    now = now or time.time()
    sched = rec[3]
    out = []

    def alive(at):  # session-only: gone if Claude restarted since
        return not started or at >= started - 5

    for j in (sched.get("jobs") or {}).values():
        if not j.get("durable") and not alive(j.get("at", 0)):
            continue
        if j.get("recurring", True):
            if now > j.get("at", 0) + j.get("days", DEFAULT_EXPIRY_DAYS) * 86400:
                continue
            out.append({"kind": "cron", "schedule": j.get("human") or j.get("cron", ""), "prompt": j.get("prompt", ""),
                        "next": None})
        else:
            fire = j.get("next") or _once_at(j.get("cron", ""), j.get("at", 0))
            if now > (fire + 60 if fire else j.get("at", 0) + 86400):  # unknown time: give it a day
                continue
            label = "once at " + time.strftime("%-I:%M%p", time.localtime(fire)).lower() if fire else "once"
            out.append({"kind": "once", "schedule": label, "prompt": j.get("prompt", ""), "next": fire})
    w = sched.get("wakeup")
    if w and alive(w.get("set", 0)) and (now < w["at"] or (busy and now - w["at"] < WAKEUP_GRACE_S)):
        out.append({"kind": "wakeup", "schedule": "/loop", "prompt": w.get("prompt", ""), "next": w["at"]})
    return out


def summary(session_id):
    """{"title", "user", "assistant"} (any may be empty)."""
    if codex.knows(session_id):
        return codex.summary(session_id)
    path = find(session_id)
    if not path:
        return {}
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_size, st.st_mtime_ns)
    if session_id in _cache and _cache[session_id][0] == key:
        return _cache[session_id][1]
    out = {"title": "", "user": "", "assistant": ""}
    try:
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return {}
    lines = data.splitlines()
    if st.st_size > TAIL_BYTES:
        lines = lines[1:]  # first line is probably cut
    for line in reversed(lines):
        if out["title"] and out["user"] and out["assistant"]:
            break
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t in ("custom-title", "ai-title") and not out["title"]:
            out["title"] = d.get("customTitle") or d.get("aiTitle") or ""
        elif t == "assistant" and not out["assistant"] and not d.get("isSidechain"):
            out["assistant"] = _text((d.get("message") or {}).get("content")).strip()
        elif t == "user" and not out["user"] and not d.get("isSidechain") and not d.get("isMeta"):
            out["user"] = _clean_user(_text((d.get("message") or {}).get("content")))
    _cache[session_id] = (key, out)
    return out
