"""Just enough markdown for the preview: what agents write in their replies.

render(md, w) -> lines, each a list of (text, style) segments that fit in w columns. Styles:
  text, bold, em, code, head, quote, dim (markers, rules), border (table lines), codeblock

Handles headings, **bold** / *em* / `code` / [links](url), bullets and numbered lists with
hanging indents, quotes, rules, fenced code, and tables: aligned columns that wrap inside
their cells, or "header: value" lines per row when the pane is too narrow for the columns.
"""
import re
import unicodedata

MIN_COL = 8


def cell_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(s):
    return sum(cell_width(c) for c in s)


def clean(s):
    return "".join(c if c.isprintable() else " " for c in s)


def cut(s, w):
    """s cut to w columns, with "…" when cut."""
    if width(s) <= w:
        return s
    out, n = [], 0
    for c in s:
        if n + cell_width(c) > w - 1:
            break
        out.append(c)
        n += cell_width(c)
    return "".join(out) + "…"


# ---- inline ---------------------------------------------------------------------------------

_INLINE = re.compile(r"(`+)(.+?)\1"                      # `code`
                     r"|\*\*(.+?)\*\*|__(.+?)__"          # **bold** __bold__
                     r"|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"   # *em*
                     r"|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])"     # _em_
                     r"|~~(.+?)~~"                         # ~~strike~~ (markers dropped)
                     r"|!?\[([^\]]+)\]\([^)\s]*\)")        # [text](url), ![alt](src)


def inline(text, style="text"):
    """[(text, style)] for one line of markdown. `style` is the base style (bold in headers)."""
    out, pos = [], 0
    text = clean(text)
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], style))
        code, bold, bold2, em, em2, strike, link = m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), \
            m.group(7), m.group(8)
        if code is not None:
            out.append((code, "code"))
        elif bold is not None or bold2 is not None:
            out += [(t, "bold" if s in ("text", "em") else s) for t, s in inline(bold or bold2, style)]
        elif em is not None or em2 is not None:
            out += [(t, "em" if s == "text" else s) for t, s in inline(em or em2, style)]
        else:
            out += inline(strike if strike is not None else link, style)
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], style))
    return [(t, s) for t, s in out if t]


def plain_width(segs):
    return sum(width(t) for t, _ in segs)


def wrap(segs, w, first="", rest="", prefix_style="dim"):
    """Word-wrap segments to w columns; `first` / `rest` are prefixes (bullet / hanging indent)."""
    w = max(4, w)
    words = []  # (word, style, space before)
    for t, s in segs:
        for i, part in enumerate(re.split(r"(\s+)", t)):
            if not part:
                continue
            if part.isspace():
                words.append(("", s, True))
            else:
                words.append((part, s, False))
    lines, line, n, space = [], [], 0, False
    prefix = first

    def start():
        return [(prefix, prefix_style)] if prefix else []

    line, n = start(), width(prefix)
    for word, s, is_space in words:
        if is_space:
            space = n > width(prefix)
            continue
        ww = width(word)
        need = ww + (1 if space else 0)
        if n + need > w and n > width(prefix):
            lines.append(line)
            prefix = rest
            line, n, space = start(), width(prefix), False
            need = ww
        while ww > w - n:  # a word longer than the line: break it
            room = max(1, w - n)
            head = cut(word, room + 1)[:-1] if room < ww else word
            head = head or word[:1]
            line.append((head, s))
            lines.append(line)
            word = word[len(head):]
            ww = width(word)
            prefix = rest
            line, n, space = start(), width(prefix), False
            need = ww
        if not word:
            continue
        if space and n > width(prefix):
            line.append((" ", s))
        line.append((word, s))
        n += need
        space = False
    if plain_width(line) > width(prefix) or not lines:
        lines.append(line)
    return lines


# ---- tables -----------------------------------------------------------------------------------

_SEP_CELL = re.compile(r"^\s*:?-{1,}:?\s*$")


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [c.strip().replace("\\|", "|") for c in cells]


def is_table_start(lines, i):
    return (i + 1 < len(lines) and "|" in lines[i]
            and all(_SEP_CELL.match(c) for c in split_row(lines[i + 1])) and "-" in lines[i + 1])


def fit_columns(natural, avail):
    """Shrink the widest columns until the row fits; None if columns would get too narrow."""
    cols = list(natural)
    while sum(cols) > avail:
        i = max(range(len(cols)), key=lambda k: cols[k])
        if cols[i] <= MIN_COL:
            return None
        cols[i] -= 1
    return cols


def table(rows, w):
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    cells = [[inline(c, "bold" if ri == 0 else "text") for c in r] for ri, r in enumerate(rows)]
    natural = [max(1, max(plain_width(r[k]) for r in cells)) for k in range(n)]
    gap = 3  # " │ "
    cols = fit_columns(natural, w - gap * (n - 1)) if n > 1 else [min(natural[0], w)]
    if cols is None:  # too many columns for the pane: one "header: value" line per cell
        out = []
        labels = [plain_text(h) or f"#{k + 1}" for k, h in enumerate(cells[0])]
        for r in cells[1:]:
            for k, c in enumerate(r):
                if not c:
                    continue
                label = cut(labels[k], max(4, w // 3))
                out += wrap([(label + ":", "bold"), (" ", "text")] + c, w, rest="  ")
            out.append([])
        return out[:-1] if out and not out[-1] else out
    out = []
    rule = [("─┼─".join("─" * c for c in cols), "border")]
    wrapped_rows = [[wrap(c, cols[k]) if c else [[]] for k, c in enumerate(r)] for r in cells]
    tall = any(len(x) > 1 for r in wrapped_rows[1:] for x in r)  # rows wrap: rule between them too
    for ri, wrapped in enumerate(wrapped_rows):
        if ri > 1 and tall:
            out.append(rule)
        for li in range(max(len(x) for x in wrapped)):
            line = []
            for k in range(n):
                part = wrapped[k][li] if li < len(wrapped[k]) else []
                line += part
                pad = cols[k] - plain_width(part)
                if k < n - 1:
                    line.append((" " * pad, "text"))
                    line.append((" │ ", "border"))
            out.append(line)
        if ri == 0:
            out.append(rule)
    return out


def plain_text(segs):
    return "".join(t for t, _ in segs)


# ---- blocks -------------------------------------------------------------------------------------

_BULLET = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TASK = re.compile(r"^\[([ xX])\]\s+")


def render(md, w):
    """Styled lines for a markdown reply, at most w columns wide."""
    lines = (md or "").replace("\t", "    ").split("\n")
    out = []
    i = 0
    blank = True
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            if not blank:
                out.append([])
            blank = True
            i += 1
            continue
        blank = False
        fence = re.match(r"^\s*(```+|~~~+)", line)
        if fence:
            mark = fence.group(1)
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(mark):
                out.append([("  " + cut(clean(lines[i]), max(1, w - 2)), "codeblock")])
                i += 1
            i += 1
            continue
        if is_table_start(lines, i):
            rows = [split_row(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(split_row(lines[i]))
                i += 1
            out += table(rows, w)
            continue
        m = _HEAD.match(line)
        if m:
            out += wrap(inline(m.group(2), "head"), w)
            i += 1
            continue
        if _RULE.match(line):
            out.append([("─" * w, "dim")])
            i += 1
            continue
        m = _BULLET.match(line)
        if m:
            depth = min(len(m.group(1)) // 2, 4)
            marker = m.group(2)
            body = m.group(3)
            task = _TASK.match(body)
            if task:
                marker, body = ("☑" if task.group(1).lower() == "x" else "☐"), body[task.end():]
            elif not marker[0].isdigit():
                marker = "•"
            indent = "  " * depth
            first = f"{indent}{marker} "
            out += wrap(inline(body), w, first=first, rest=" " * width(first))
            i += 1
            continue
        if line.lstrip().startswith(">"):
            body = re.sub(r"^\s*>\s?", "", line)
            out += wrap(inline(body, "quote"), w, first="│ ", rest="│ ", prefix_style="border")
            i += 1
            continue
        out += wrap(inline(line.strip()), w)
        i += 1
    while out and not out[-1]:
        out.pop()
    return out
