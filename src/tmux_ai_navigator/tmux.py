"""Thin wrapper around the tmux CLI."""
import os
import re
import subprocess
from dataclasses import dataclass

# printable on purpose: tmux 3.2+ escapes control characters in format output ("\037")
SEP = ":~ainav~:"
PANE_FIELDS = ["pane_id", "pane_pid", "session_id", "session_name", "window_id", "window_index",
               "window_name", "pane_active", "window_active", "pane_current_path", "pane_title",
               "session_attached"]


@dataclass
class Pane:
    pane_id: str
    pane_pid: int
    session_id: str
    session_name: str
    window_id: str
    window_index: int
    window_name: str
    pane_active: bool
    window_active: bool
    pane_current_path: str
    pane_title: str = ""
    session_attached: int = 0

    @property
    def on_screen(self):
        """Shown in some client right now (active pane of the active window of an attached session)."""
        return self.session_attached > 0 and self.window_active and self.pane_active


class TmuxError(Exception):
    pass


class Tmux:
    def __init__(self, socket_name=None, socket_path=None, exe="tmux"):
        self.base = [exe]
        if socket_path:
            self.base += ["-S", socket_path]
        elif socket_name:
            self.base += ["-L", socket_name]

    def run(self, *args, check=True):
        try:
            r = subprocess.run(self.base + list(args), capture_output=True, encoding="utf-8",
                               errors="replace", timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            raise TmuxError(str(e))
        if check and r.returncode != 0:
            raise TmuxError(r.stderr.strip() or f"tmux {args[0]} failed")
        return r.stdout

    def version(self):
        """(major, minor) e.g. (3, 0) for "tmux 3.0a"; (0, 0) if unknown."""
        m = re.search(r"(\d+)\.(\d+)", self.run("-V", check=False))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    def panes(self, strict=False):
        """Every pane in every session. A pane in a linked window appears once per session.
        If tmux fails: [] (the picker shows what it can), or TmuxError when strict."""
        fmt = SEP.join("#{%s}" % f for f in PANE_FIELDS)
        try:
            out = self.run("list-panes", "-a", "-F", fmt)
        except TmuxError:
            if strict:
                raise
            return []
        panes = []
        for line in out.splitlines():
            v = line.split(SEP)
            if len(v) != len(PANE_FIELDS):
                continue
            try:
                panes.append(Pane(v[0], int(v[1]), v[2], v[3], v[4], int(v[5]), v[6],
                                  v[7] == "1", v[8] == "1", v[9], v[10], int(v[11] or 0)))
            except ValueError:
                continue
        return panes

    def server_version(self):
        if not hasattr(self, "_server_version"):
            self._server_version = self.run("display", "-p", "#{version}", check=False).strip()
        return self._server_version

    def options(self, names):
        """{name: value} of global options, in one call; "" when unset.

        tmux 3.4 prints "$" in option values as "\\$" (3.3 and 3.5 don't), which would break
        shell commands kept in options, so that's undone for 3.4.
        """
        out = self.run("display", "-p", SEP.join("#{%s}" % n for n in names), check=False)
        values = out.rstrip("\n").split(SEP)
        if len(values) != len(names):
            return {n: "" for n in names}
        if self.server_version().startswith("3.4"):
            values = [v.replace("\\$", "$") for v in values]
        return dict(zip(names, values))

    def client_format(self, tty, fmt):
        """fmt expanded for one client (its session, window and pane), or "" if it's gone.
        list-clients, because `display -p -c` is rejected by tmux 3.2."""
        out = self.run("list-clients", "-F", "#{client_tty}" + SEP + fmt, check=False)
        for line in out.splitlines():
            name, _, rest = line.partition(SEP)
            if name == tty:
                return rest
        return ""

    def server_pid(self):
        return self.run("display", "-p", "#{pid}", check=False).strip()

    def clients(self):
        out = self.run("list-clients", "-F", "#{client_tty}", check=False)
        return [l for l in out.splitlines() if l]

    def jump(self, client, session_id, window_id, pane_id):
        """Switch client to the pane's session, window and pane in one command.

        The window must be spelled out: tmux 3.0a rejects "$session:%pane" ("can't find window").
        Naming the session matters for linked windows that live in several sessions.
        """
        args = ["switch-client"]
        if client:
            args += ["-c", client]
        self.run(*args, "-t", f"{session_id}:{window_id}.{pane_id}")


def from_env():
    """Tmux bound to the server we're running inside, if any (TMUX="socket,pid,session")."""
    t = os.environ.get("TMUX", "")
    return Tmux(socket_path=t.split(",")[0]) if t else Tmux()
