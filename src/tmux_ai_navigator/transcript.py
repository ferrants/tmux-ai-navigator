"""Last user prompt, last assistant reply and title from a Claude transcript (.jsonl).

Only the tail of the file is read, and results are cached by (path, size, mtime), so this
is cheap enough to call for the selected row on every redraw.
"""
import glob
import json
import os
import re

from . import claude

TAIL_BYTES = 512 * 1024
_paths = {}
_cache = {}
_titles = {}  # path -> (bytes scanned, custom title, ai title); persisted between runs
_titles_dirty = False


def _cache_file():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "tmux-ai-navigator", "titles.json")


def load_title_cache():
    try:
        with open(_cache_file()) as f:
            data = json.load(f)
        for path, v in data.items():
            if isinstance(v, list) and len(v) == 3 and path not in _titles:
                _titles[path] = tuple(v)
    except (OSError, ValueError, AttributeError):
        pass


def save_title_cache():
    """Write the scan positions back (only live files, only if something changed)."""
    global _titles_dirty
    if not _titles_dirty:
        return
    path = _cache_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({p: list(v) for p, v in _titles.items() if os.path.exists(p)}, f)
        os.replace(tmp, path)
        _titles_dirty = False
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


def titles(session_id):
    """(custom title, ai title), the latest of each anywhere in the transcript.

    Titles can sit megabytes back, so the file is scanned once and then only new bytes are read.
    """
    path = find(session_id)
    if not path:
        return "", ""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", ""
    done, custom, ai = _titles.get(path, (0, "", ""))
    if size < done:
        done, custom, ai = 0, "", ""
    if size > done:
        try:
            with open(path, "rb") as f:
                f.seek(done)
                data = f.read(size - done)
        except OSError:
            return custom, ai
        end = data.rfind(b"\n") + 1  # leave a partial last line for next time
        for line in data[:end].split(b"\n"):
            if b'Title"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "custom-title" and d.get("customTitle"):
                custom = d["customTitle"]
            elif d.get("type") == "ai-title" and d.get("aiTitle"):
                ai = d["aiTitle"]
        _titles[path] = (done + end, custom, ai)
        global _titles_dirty
        _titles_dirty = True
    return custom, ai


def summary(session_id):
    """{"title", "user", "assistant"} (any may be empty)."""
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
