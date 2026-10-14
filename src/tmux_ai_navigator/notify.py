"""Notifications when an agent finishes or needs you, from a small background watcher.

`tmux-ai-nav watch --start` (run by the plugin) starts one detached watcher per tmux server.
It polls Claude's session files (only changed files are re-read) and, when a session goes
from working to idle ("done") or starts waiting for you, waits `delay` seconds, checks the
session is still in that state and its pane isn't on screen, then notifies.

Options, re-read every few seconds (tmux `set -g`):
  @ainav-notify          comma list of: tmux, system, terminal, bell; or off   (default tmux)
  @ainav-notify-on       which events: done, waiting                           (default both)
  @ainav-notify-sound    on, off, or a sound file for both events              (default off)
  @ainav-notify-sound-done / @ainav-notify-sound-waiting   per-event sound file
  @ainav-notify-delay    seconds the state must hold first                     (default 1)
  @ainav-notify-visible  on: also notify for panes that are on screen          (default off)
  @ainav-notify-command  run this too, with AINAV_* variables in the environment
"""
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time


POLL_S = 0.5
OPTIONS_S = 5.0
SOUNDS = {
    "linux": {"done": "/usr/share/sounds/freedesktop/stereo/complete.oga",
              "waiting": "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"},
    "darwin": {"done": "/System/Library/Sounds/Glass.aiff", "waiting": "/System/Library/Sounds/Ping.aiff"},
}
DEFAULTS = {"notify": "tmux", "notify-on": "done,waiting", "notify-sound": "off", "notify-sound-done": "",
            "notify-sound-waiting": "", "notify-delay": "1", "notify-visible": "off", "notify-command": ""}


# ---- detecting events --------------------------------------------------------------------

def snapshot(entries):
    """(pid, session id) -> (status, statusUpdatedAt s, startedAt s) for interactive sessions.

    Keyed by process too: a resumed session gets a new process (and file) while the old
    file may linger, and the stale one must not mask the live one's changes.
    """
    out = {}
    for d in entries:
        sid, pid = d.get("sessionId"), d.get("pid")
        if not isinstance(sid, str) or not isinstance(pid, int) or d.get("kind", "interactive") != "interactive":
            continue
        since, started = d.get("statusUpdatedAt"), d.get("startedAt")
        out[(pid, sid)] = (d.get("status") if isinstance(d.get("status"), str) else "",
                    since / 1000.0 if isinstance(since, (int, float)) else 0.0,
                    started / 1000.0 if isinstance(started, (int, float)) else 0.0)
    return out


def events(prev, cur):
    """[((pid, session id), "done"|"waiting")] for state changes between two snapshots."""
    out = []
    for key, (status, since, started) in cur.items():
        if key not in prev:
            continue  # new file or first look: nothing changed that we saw
        old = prev[key][0]
        if status == "waiting" and old != "waiting":
            out.append((key, "waiting"))
        elif old == "busy" and status in ("idle", "shell") and not (started and since - started < 10):
            out.append((key, "done"))
    return out


def still(kind, status):
    return status == "waiting" if kind == "waiting" else status in ("idle", "shell")


# ---- delivering ---------------------------------------------------------------------------

def options(tmux):
    """@ainav-notify* options merged over the defaults, exact values in one tmux call
    (`show -g` would quote and escape them)."""
    opts = dict(DEFAULTS)
    names = list(DEFAULTS)
    try:
        out = tmux.run("display", "-p", "\x1f".join(f"#{{@ainav-{n}}}" for n in names), check=False)
    except Exception:
        return opts
    values = out.rstrip("\n").split("\x1f")
    if len(values) == len(names):
        for n, v in zip(names, values):
            if v.strip():
                opts[n] = v.strip()
    return opts


def message(agent, kind):
    where = "/".join(x for x in (agent.tag, agent.window_label) if x)
    if kind == "waiting":
        return f"◉ needs you · {where}", agent.title
    return f"✓ done · {where}", agent.title


def sound_file(kind, opts):
    path = opts.get(f"notify-sound-{kind}") or ""
    setting = opts.get("notify-sound", "off")
    if not path and setting not in ("off", "on", ""):
        path = setting
    if not path and setting == "on":
        path = SOUNDS.get("darwin" if sys.platform == "darwin" else "linux", {}).get(kind, "")
    return os.path.expanduser(path)


def play(path):
    if not path or not os.path.exists(path):
        return
    for player in (["afplay"], ["paplay"], ["pw-play"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(player[0]):
            spawn(player + [path])
            return


def system_notify(title, body):
    if sys.platform == "darwin":
        if shutil.which("terminal-notifier"):
            spawn(["terminal-notifier", "-title", title, "-message", body or " ", "-group", "tmux-ai-navigator"])
        else:
            spawn(["osascript", "-e", "on run argv", "-e",
                   "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                   title, body or " "])
    elif shutil.which("notify-send"):
        spawn(["notify-send", "-a", "tmux-ai-navigator", title, body or " "])


def write_ttys(ttys, data):
    """Write straight to the outer terminals: tmux 3.0 has no way to pass these through."""
    for tty in ttys:
        try:
            fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError:
            pass


def osc_notify(title, body):
    clean = lambda s: "".join(c for c in s if c >= " " and c not in ";\x7f")  # noqa: E731
    return f"\x1b]777;notify;{clean(title)};{clean(body)}\x1b\\".encode()


def spawn(cmd, env=None):
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    except OSError:
        pass


def deliver(tmux, agent, kind, opts):
    title, body = message(agent, kind)
    methods = {m.strip() for m in opts.get("notify", "").split(",")}
    ttys = tmux.clients() if methods & {"terminal", "bell"} else []
    if "tmux" in methods:
        for tty in tmux.clients():
            tmux.run("display-message", "-c", tty, f"{title}: {body}", check=False)
    if "system" in methods:
        system_notify(title, body)
    if "terminal" in methods:
        write_ttys(ttys, osc_notify(title, body))
    if "bell" in methods:
        write_ttys(ttys, b"\a")
    play(sound_file(kind, opts))
    cmd = opts.get("notify-command")
    if cmd:
        env = dict(os.environ, AINAV_EVENT=kind, AINAV_TITLE=agent.title, AINAV_PROJECT=agent.tag,
                   AINAV_WINDOW=agent.window_name or "", AINAV_PANE=agent.pane_id or "",
                   AINAV_TMUX_SESSION=agent.tmux_session or "", AINAV_SESSION_ID=agent.session_id,
                   AINAV_CWD=agent.cwd, AINAV_MESSAGE=f"{title}: {body}")
        spawn(["sh", "-c", cmd], env=env)


# ---- the watcher ----------------------------------------------------------------------------

class Watcher:
    def __init__(self, discovery, deliver_fn=deliver, clock=time.time):
        self.disc = discovery
        self.deliver = deliver_fn
        self.clock = clock
        self.prev = None
        self.pending = {}  # (pid, session id) -> (kind, due)
        self.opts = dict(DEFAULTS)
        self._opts_at = 0.0

    def step(self):
        now = self.clock()
        if now - self._opts_at >= OPTIONS_S:
            self.opts, self._opts_at = options(self.disc.tmux), now
        cur = snapshot(self.disc.files.read())
        if self.prev is not None:
            wanted = {e.strip() for e in self.opts.get("notify-on", "").split(",")}
            try:
                delay = max(0.0, float(self.opts.get("notify-delay") or 0))
            except ValueError:
                delay = 1.0
            for key, kind in events(self.prev, cur):
                if kind in wanted:
                    self.pending[key] = (kind, now + delay)
        self.prev = cur
        # the state has to hold for the whole delay: drop events whose session moved on
        self.pending = {key: (kind, at) for key, (kind, at) in self.pending.items()
                        if key in cur and still(kind, cur[key][0])}
        due = {key: kind for key, (kind, at) in self.pending.items() if at <= now}
        if not due:
            return []
        for key in due:
            del self.pending[key]
        sent = []
        show_visible = self.opts.get("notify-visible") == "on"
        for a in self.disc.agents(record_seen=False):
            kind = due.get((a.pid, a.session_id))
            if kind and still(kind, a.status) and (show_visible or not a.on_screen):
                self.deliver(self.disc.tmux, a, kind, self.opts)
                sent.append((a.session_id, kind))
        return sent

    def run(self, alive):
        while alive():
            try:
                self.step()
            except Exception:
                pass  # keep watching; a bad moment (tmux busy, file mid-write) shouldn't end it
            time.sleep(POLL_S)


def lock_path(directory, server_pid):
    return os.path.join(directory, f"watch-{server_pid}.lock")


def stop_existing(directory, server_pid):
    """Ask a running watcher for this server to exit (re-sourcing the config restarts it).

    Only a pid from a lock that is actually held counts: a watcher that died leaves its pid
    behind, and that pid may belong to some other process by now.
    """
    try:
        f = open(lock_path(directory, server_pid))
    except OSError:
        return
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return  # nobody holds it: stale
        except OSError:
            pass
        try:
            pid = int(f.read().strip() or 0)
        except ValueError:
            return
    if pid > 1 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def hold_lock(directory, server_pid, wait=3.0):
    """Become the watcher for this server: an exclusive lock held for the process lifetime."""
    os.makedirs(directory, exist_ok=True)
    f = open(lock_path(directory, server_pid), "a+")
    end = time.time() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > end:
                f.close()
                return None
            time.sleep(0.1)
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


def server_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def clean_stale_locks(directory, keep):
    """Drop lock files left by watchers of tmux servers that are gone."""
    d = directory
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if n.startswith("watch-") and n.endswith(".lock"):
            try:
                pid = int(n[len("watch-"):-len(".lock")])
            except ValueError:
                continue
            if pid != keep and not server_alive(pid):
                try:
                    os.remove(os.path.join(d, n))
                except OSError:
                    pass


def watch(discovery):
    server, directory = int(discovery.server()), discovery.state.dir
    lock = hold_lock(directory, server)
    if lock is None:
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    clean_stale_locks(directory, server)
    try:
        Watcher(discovery).run(lambda: server_alive(server))
    finally:
        lock.close()
    return 0

