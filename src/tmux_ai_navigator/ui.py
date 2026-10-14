"""curses picker: sectioned agent list with a preview; type to search, Enter to jump."""
import curses
import os
import re
import textwrap
import time
import unicodedata
import zlib

from . import transcript
from .agents import arrange, fmt_age

FILTERS = [
    ("all", lambda a: True),
    ("needs you", lambda a: a.section() == "needs"),
    ("working", lambda a: a.status == "busy"),
    ("idle", lambda a: a.status == "idle" and not a.done),
]
MODES = ["priority", "project"]
KIND_MARKS = {"codex": "cx"}  # shown on rows of agents other than Claude Code
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TICK_MS = 250
REFRESH_S = 1.0
OLDER = ("more", "older")
BADGE_COLORS = {"warn": "err", "info": "accent", "dim": "dim"}

HELP = "enter jump · type to search · ↑↓ move · ⇥ filter · ^s group · ^l later · esc clear/quit"

# Tokyo Night (night), as the nearest xterm-256 colors; -1/None = terminal default
TOKYO_NIGHT = {
    "text": 153,     # fg       #c0caf5
    "name": 153,     # window names, bold
    "muted": 146,    # fg_dark  #a9b1d6
    "dim": 67,       # dark5    #737aa2
    "line": 60,      # comment  #565f89
    "head": 111,     # blue     #7aa2f7
    "accent": 111,
    "wait": 215,     # orange   #ff9e64
    "later": 179,    # yellow   #e0af68
    "busy": 117,     # cyan     #7dcfff
    "done": 149,     # green    #9ece6a
    "prompt": 141,   # purple   #bb9af7
    "err": 210,      # red      #f7768e
    "you": 111,
    "claude": 141,
    "sel_bg": 236,   # bg_highlight #292e42
    "tags": (111, 141, 80, 179, 215, 149, 117, 210),
}
ANSI = {
    "text": None, "name": None, "muted": None, "dim": curses.COLOR_WHITE, "line": curses.COLOR_WHITE,
    "head": curses.COLOR_CYAN, "accent": curses.COLOR_CYAN, "wait": curses.COLOR_YELLOW,
    "later": curses.COLOR_YELLOW, "busy": curses.COLOR_CYAN, "done": curses.COLOR_GREEN,
    "prompt": curses.COLOR_GREEN, "err": curses.COLOR_RED, "you": curses.COLOR_BLUE,
    "claude": curses.COLOR_MAGENTA, "sel_bg": -1,
    "tags": (curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_YELLOW, curses.COLOR_GREEN,
             curses.COLOR_BLUE, curses.COLOR_RED),
}


# ---- text helpers -------------------------------------------------------------------------

def cell_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(s):
    return sum(cell_width(c) for c in s)


def fit(s, w, pad=False):
    """Cut s to w display columns (with "…" when cut); optionally pad to exactly w."""
    if w <= 0:
        return ""
    if width(s) > w:
        out, n = [], 0
        for c in s:
            cw = cell_width(c)
            if n + cw > w - 1:
                break
            out.append(c)
            n += cw
        s = "".join(out) + "…"
    return s + " " * (w - width(s)) if pad else s


def clean(s):
    return "".join(c if c.isprintable() else " " for c in (s or ""))


def plain(md):
    """Light markdown cleanup for the preview: drop emphasis/code markers and blank runs."""
    md = re.sub(r"```.*?```", "[code]", md or "", flags=re.S)
    md = re.sub(r"\*\*|__|`", "", md)
    md = re.sub(r"^#+\s*", "", md, flags=re.M)
    md = re.sub(r"\n\s*\n+", "\n", md)
    return "\n".join(clean(l) for l in md.split("\n"))


def wrap(text, w, limit):
    """Wrap to at most `limit` lines of width w, marking the cut with "…"."""
    out = []
    for para in text.split("\n"):
        out += textwrap.wrap(para, max(10, w), break_long_words=True, replace_whitespace=False) or [""]
        if len(out) > limit:
            break
    if len(out) > limit:
        out = out[:limit]
        out[-1] = fit(out[-1] + " …", w)
    return out


# ---- model ---------------------------------------------------------------------------------

def matches(agent, query):
    terms = query.split()
    if not terms:
        return True
    smart = any(c.isupper() for c in query)
    hay = agent.search_text()
    if not smart:
        hay, terms = hay.lower(), [t.lower() for t in terms]
    return all(t in hay for t in terms)


def agent_key(a):
    return (a.pid, a.session_id)


def build_items(agents, query, filter_idx, mode, show_older, now=None):
    """Display items: ("header", key, title, n) | ("agent", agent) | ("more", n).

    The OLDER section folds into one "more" row unless expanded or searching.
    """
    pred = FILTERS[filter_idx][1]
    shown = [a for a in agents if pred(a) and matches(a, query)]
    items = []
    for key, title, group in arrange(shown, mode, now):
        items.append(("header", key, title, len(group)))
        if key == "older" and not (show_older or query):
            items.append(("more", len(group)))
        else:
            items += [("agent", a) for a in group]
    return items, shown


def item_key(it):
    return agent_key(it[1]) if it[0] == "agent" else OLDER if it[0] == "more" else None


class Picker:
    def __init__(self, load, here_pane=None, toggle_later=None, summarize=None, warning=None):
        self.load = load                # () -> list[Agent]
        self.toggle_later_cb = toggle_later
        self.summarize = summarize or transcript.summary  # session id -> {"user", "assistant"}
        self.warning = warning          # () -> problem to show in the footer, or ""
        self.here = here_pane
        self.agents = []
        self.shown = []
        self.query = ""
        self.filter_idx = 0
        self.mode_idx = 0
        self.show_older = False
        self.sel_key = None
        self.top = 0
        self.error = ""
        self.flash = ""

    def refresh(self):
        try:
            self.agents = self.load()
            self.error = (self.warning() if self.warning else "") or ""
        except Exception as e:  # keep the UI alive; show the problem in the status line
            self.error = f"refresh failed: {e}"

    # ---- drawing ------------------------------------------------------------------------

    def colors(self):
        """Tokyo Night on 256-color terminals (nearest xterm-256 entries, since tmux 3.0a won't
        pass truecolor through curses); plain ANSI colors and reverse video otherwise."""
        curses.use_default_colors()
        rich = curses.COLORS >= 256
        pal = TOKYO_NIGHT if rich else ANSI
        n = 0

        def pair(fg, bg=-1):
            nonlocal n
            if fg is None and bg == -1:
                return 0
            n += 1
            curses.init_pair(n, -1 if fg is None else fg, bg)
            return curses.color_pair(n)

        sel_bg = pal["sel_bg"]
        self.c, self.cs = {}, {}  # normal, and on the selected row
        for k, fg in pal.items():
            if k in ("sel_bg", "tags"):
                continue
            self.c[k] = pair(fg)
            self.cs[k] = pair(fg, sel_bg) if rich else self.c[k] | curses.A_REVERSE
        self.tag_colors = [pair(fg) for fg in pal["tags"]]
        self.tag_colors_sel = ([pair(fg, sel_bg) for fg in pal["tags"]] if rich
                               else [t | curses.A_REVERSE for t in self.tag_colors])

    def tag_attr(self, tag, chosen=False):
        colors = self.tag_colors_sel if chosen else self.tag_colors
        return colors[zlib.crc32(tag.encode()) % len(colors)]

    def icon(self, a, c=None):
        c = c or self.c
        if a.status == "waiting":
            return "◉", c["wait"] | curses.A_BOLD
        if a.done:
            return "✓", c["done"] | curses.A_BOLD
        if a.status == "busy":
            return SPINNER[int(time.time() * 4) % len(SPINNER)], c["busy"] | curses.A_BOLD
        if a.later:
            return "⚑", c["later"]
        if a.status == "shell":
            return "$", c["busy"]
        return "○", c["dim"]

    def put(self, y, x, text, attr=0):
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def draw_row(self, y, x0, w, it, chosen, now, win_w=0):
        c = self.cs if chosen else self.c
        self.put(y, x0, " " * w, c["text"])
        if chosen:
            self.put(y, x0, "▌", c["accent"])
        if it[0] == "more":
            self.put(y, x0 + 3, fit(f"▸ {it[1]} older than a day · enter to show", w - 4), c["dim"])
            return
        a = it[1]
        ch, attr = self.icon(a, c)
        self.put(y, x0 + 1, ch, attr)
        age = fmt_age(a.age(now))
        tag = fit(a.tag, 10)
        badge = fit(clean(a.badge), 8)
        mark = KIND_MARKS.get(a.kind, "")
        right_w = width(tag) + 1 + 5 + (width(badge) + 1 if badge else 0) + (len(mark) + 1 if mark else 0)
        title = clean(a.title) + (" (here)" if a.pane_id and a.pane_id == self.here else "")
        bold = curses.A_BOLD if a.section(now) in ("needs", "working") else 0
        x = x0 + 3
        room = w - 4 - right_w - 2
        if win_w:
            # tmux window name first: it's how windows are named and found day to day
            self.put(y, x, fit(clean(a.window_label), win_w), c["name"] | curses.A_BOLD)
            x += win_w + 2
            room -= win_w + 2
        same = a.window_label and a.window_label.lower() == a.title.lower()
        title_attr = (c["text"] | bold) if not win_w else c["text"] if bold else c["muted"]
        self.put(y, x, fit("" if same else title, room), title_attr)
        x = x0 + w - right_w - 1
        if mark:
            self.put(y, x, mark, c["muted"])
            x += len(mark) + 1
        if badge:
            self.put(y, x, badge, c[BADGE_COLORS.get(a.badge_style, "dim")])
            x += width(badge) + 1
        self.put(y, x, tag, self.tag_attr(a.tag, chosen))
        self.put(y, x + width(tag) + 1, age.rjust(5), c["dim"])

    def draw_header(self, w, counts):
        self.put(0, 1, "agents", self.c["head"] | curses.A_BOLD)
        x = 10
        for key, label, attr in (("needs", "need you", self.c["wait"] | curses.A_BOLD),
                                 ("working", "working", self.c["busy"]),
                                 ("today", "today", self.c["text"]), ("older", "older", self.c["dim"])):
            n = counts.get(key, 0)
            if not n:
                continue
            s = f"{n} {label}"
            self.put(0, x, s, attr)
            x += width(s) + 3
        right = f"filter: {FILTERS[self.filter_idx][0]} · {MODES[self.mode_idx]}"
        if x + width(right) + 2 < w:
            self.put(0, w - width(right) - 1, right, self.c["dim"])

    def draw_preview(self, a, x, y, w, h, now):
        if not a or w < 20 or h < 3:
            return
        lines = []  # (text, attr)
        for t in wrap(clean(a.title), w, 2):
            lines.append((t, self.c["text"] | curses.A_BOLD))
        home = os.path.expanduser("~")
        cwd = "~" + a.cwd[len(home):] if a.cwd.startswith(home) else a.cwd
        lines.append((cwd + (f" · {a.branch}" if a.branch else ""), self.c["dim"]))
        loc = f"{a.location} {clean(a.window_name or '')}  ·  {a.pane_id or '-'}"
        if a.other_sessions:
            loc += f"  ·  also in {', '.join(a.other_sessions)}"
        lines.append((loc, self.c["dim"]))
        age = fmt_age(a.age(now))
        if a.status == "waiting":
            st, attr = f"◉ waiting for you · {age}", self.c["wait"] | curses.A_BOLD
        elif a.done:
            st, attr = f"✓ finished {age} ago", self.c["done"] | curses.A_BOLD
        elif a.status == "busy":
            st, attr = f"working · {age}", self.c["busy"]
        elif a.status == "shell":
            st, attr = f"$ shell running · {age}", self.c["busy"]
        else:
            st, attr = f"idle · {age}", self.c["dim"]
        if a.later:
            st += "  ⚑ later"
        lines.append((st, attr))
        if a.extra_lines:
            lines.append(("", 0))
            for n in a.extra_lines:
                lines += [(t, self.c["muted"]) for t in wrap(clean(n), w, 2)]
        s = self.summarize(a.session_id)
        if s.get("user"):
            lines.append(("", 0))
            lines.append(("you ›", self.c["you"] | curses.A_BOLD))
            lines += [("  " + t, self.c["text"]) for t in wrap(plain(s["user"]), w - 2, 3)]
        if s.get("assistant"):
            lines.append(("", 0))
            lines.append((f"{a.kind} ›", self.c["claude"] | curses.A_BOLD))
            room = max(1, h - len(lines))
            lines += [("  " + t, self.c["muted"]) for t in wrap(plain(s["assistant"]), w - 2, room)]
        for i, (text, attr) in enumerate(lines[:h]):
            self.put(y + i, x, fit(text, w), attr)

    def draw(self, items, sel, now):
        scr = self.scr
        h, w = scr.getmaxyx()
        scr.erase()
        counts = {}
        for a in self.shown:
            k = a.section(now)
            counts[k] = counts.get(k, 0) + 1
        self.draw_header(w, counts)

        side = w >= 110
        list_w = max(60, min(100, w * 11 // 20)) if side else w
        bottom_h = 0 if side or h < 20 else min(12, h // 3)
        top, list_h = 3, max(1, h - 4 - bottom_h)

        # keep the selection on screen, with its section header when there's room
        if sel is not None:
            if sel < self.top:
                self.top = sel - 1 if sel > 0 and items[sel - 1][0] == "header" else sel
            elif sel >= self.top + list_h:
                self.top = sel - list_h + 1
        self.top = max(0, min(self.top, max(0, len(items) - list_h)))

        labels = [width(clean(it[1].window_label)) for it in items if it[0] == "agent"]
        win_w = min(24, max(labels)) if labels and max(labels) else 0
        for i, it in enumerate(items[self.top:self.top + list_h]):
            y = top + i
            if it[0] == "header":
                attr = {"needs": self.c["wait"], "working": self.c["busy"]}.get(it[1], self.c["head"])
                if it[1].startswith("p:"):
                    attr = self.tag_attr(it[1][2:])
                self.put(y, 1, it[2], attr | curses.A_BOLD)
                self.put(y, 2 + width(it[2]), str(it[3]), self.c["dim"])
            else:
                self.draw_row(y, 0, list_w, it, self.top + i == sel, now, win_w)
        if not items:
            msg = "no agents match" if self.agents else "no Claude sessions found in tmux"
            self.put(top, 3, msg, self.c["dim"])

        chosen = items[sel][1] if sel is not None and items[sel][0] == "agent" else None
        if side:
            for y in range(2, h - 1):
                self.put(y, list_w + 1, "│", self.c["line"])
            self.draw_preview(chosen, list_w + 3, top, w - list_w - 4, h - top - 1, now)
        elif bottom_h:
            y0 = h - 1 - bottom_h
            self.put(y0, 0, "─" * (w - 1), self.c["line"])
            self.draw_preview(chosen, 1, y0 + 1, w - 2, bottom_h - 1, now)

        foot = self.error or self.flash or HELP
        self.put(h - 1, 1, fit(foot, w - 2), self.c["err"] if self.error else self.c["dim"])
        self.put(1, 1, "›", self.c["prompt"] | curses.A_BOLD)
        if self.query:
            self.put(1, 3, fit(self.query, w - 4), self.c["text"])
        else:
            self.put(1, 3, "type to search", self.c["dim"])
        try:
            scr.move(1, min(w - 1, 3 + width(self.query)))
        except curses.error:
            pass
        scr.refresh()
        return list_h

    # ---- loop ---------------------------------------------------------------------------

    def run(self, scr):
        self.scr = scr
        self.colors()
        # raw, not cbreak: with flow control (IXON) on, ctrl-s would pause output instead of
        # reaching us; ctrl-c arrives as "\x03" and is handled below
        curses.raw()
        curses.curs_set(1)
        scr.keypad(True)
        scr.timeout(TICK_MS)
        self.refresh()
        last = time.time()
        while True:
            now = time.time()
            items, self.shown = build_items(self.agents, self.query, self.filter_idx,
                                            MODES[self.mode_idx], self.show_older, now)
            selectable = [i for i, it in enumerate(items) if it[0] != "header"]
            keys = [item_key(items[i]) for i in selectable]
            if self.sel_key not in keys:
                self.sel_key = keys[0] if keys else None
            sel = selectable[keys.index(self.sel_key)] if self.sel_key is not None else None
            list_h = self.draw(items, sel, now)
            try:
                k = scr.get_wch()
            except curses.error:  # tick: redraw (spinner), refresh data once a second
                k = None
            except KeyboardInterrupt:
                return None
            if time.time() - last >= REFRESH_S:
                self.refresh()
                last = time.time()
            if k is None:
                continue
            self.flash = ""
            pos = keys.index(self.sel_key) if self.sel_key in keys else 0
            move, reset = 0, False
            if k == "\x1b":
                if not self.query:
                    return None
                self.query, reset = "", True
            elif k in ("\x03", "\x07"):
                return None
            elif k in ("\n", "\r", curses.KEY_ENTER):
                if sel is None:
                    continue
                if items[sel][0] == "more":
                    self.show_older = True
                    continue
                return items[sel][1]
            elif k in (curses.KEY_UP, "\x10"):
                move = -1
            elif k in (curses.KEY_DOWN, "\x0e"):
                move = 1
            elif k == curses.KEY_PPAGE:
                move = -list_h
            elif k == curses.KEY_NPAGE:
                move = list_h
            elif k == curses.KEY_HOME:
                move = -len(keys)
            elif k == curses.KEY_END:
                move = len(keys)
            elif k == "\t":
                self.filter_idx, reset = (self.filter_idx + 1) % len(FILTERS), True
            elif k == curses.KEY_BTAB:
                self.filter_idx, reset = (self.filter_idx - 1) % len(FILTERS), True
            elif k == "\x13":
                self.mode_idx, reset = (self.mode_idx + 1) % len(MODES), True
            elif k == "\x0c":
                if sel is not None and items[sel][0] == "agent" and self.toggle_later_cb:
                    a = items[sel][1]
                    on = self.toggle_later_cb(a)
                    self.flash = f"{'flagged' if on else 'unflagged'} for later: {a.title}"
                    self.refresh()
                    last = time.time()
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                self.query, reset = self.query[:-1], True
            elif k == "\x15":
                self.query, reset = "", True
            elif k == "\x17":
                q = self.query.rstrip()
                self.query, reset = q[:len(q) - len(q.split(" ")[-1])], True
            elif isinstance(k, str) and k.isprintable():
                self.query, reset = self.query + k, True
            if move and keys:
                self.sel_key = keys[max(0, min(len(keys) - 1, pos + move))]
            elif reset:
                self.sel_key, self.top = None, 0


def has_terminfo(term):
    dirs = [os.environ.get("TERMINFO"), os.path.expanduser("~/.terminfo"), "/etc/terminfo",
            "/lib/terminfo", "/usr/share/terminfo", "/usr/lib/terminfo"]
    dirs += (os.environ.get("TERMINFO_DIRS") or "").split(":")
    return any(d and (os.path.exists(os.path.join(d, term[0], term))
                      or os.path.exists(os.path.join(d, "%02x" % ord(term[0]), term)))
               for d in dirs)


def upgrade_term():
    """Inside tmux, a plain "screen"/"tmux" TERM limits curses to 8 colors. tmux itself
    understands 256 colors and maps them down for clients that can't show them."""
    term = os.environ.get("TERM", "")
    if os.environ.get("TMUX") and term in ("screen", "tmux") and has_terminfo(term + "-256color"):
        os.environ["TERM"] = term + "-256color"


def pick(load, here_pane=None, toggle_later=None, summarize=None, warning=None):
    os.environ.setdefault("ESCDELAY", "25")
    upgrade_term()
    return curses.wrapper(Picker(load, here_pane, toggle_later, summarize, warning).run)
