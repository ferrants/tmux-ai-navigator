"""Process table helpers: parent pid, start time token, and this machine's pid domain.

Claude records its own start time in each session file ("procStart") so a reused pid can be
told apart. The token is platform specific, and we must produce exactly the same string:
  Linux:  field 22 of /proc/<pid>/stat (start time in clock ticks since boot)
  macOS:  `ps -o lstart= -p <pid>` with LC_ALL=C TZ=UTC, e.g. "Mon Oct 13 18:49:01 2026"
"""
import calendar
import os
import subprocess
import sys
import time

PROC = "/proc"


def read_stat(pid, proc=PROC):
    """(ppid, starttime) for pid from /proc/<pid>/stat, or None if it can't be read.

    The command name (field 2) is wrapped in parentheses and may itself contain spaces or
    parentheses, so fields are counted from the last ")".
    """
    try:
        with open(os.path.join(proc, str(pid), "stat")) as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    end = data.rfind(")")
    if end < 0:
        return None
    rest = data[end + 2:].split()
    # rest[0] is field 3 (state), so field N is rest[N - 3]
    try:
        return int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None


class ProcTable:
    """Linux: reads /proc on demand."""

    def __init__(self, root=PROC):
        self.root = root

    def info(self, pid):
        """(ppid, start token as a string) or None if the process doesn't exist."""
        st = read_stat(pid, self.root)
        return (st[0], str(st[1])) if st else None


class PsTable:
    """Everywhere else (macOS): one `ps` snapshot of every process."""

    def __init__(self, output=None):
        if output is None:
            output = ps_output()
        self.procs = parse_ps(output)

    def info(self, pid):
        return self.procs.get(pid)


def ps_output():
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    try:
        return subprocess.run(["ps", "-A", "-o", "pid=,ppid=,lstart="], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_ps(output):
    """pid -> (ppid, lstart). lstart keeps its inner spacing ("Oct  3"), as Claude stores it."""
    procs = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            procs[int(parts[0])] = (int(parts[1]), parts[2].strip())
        except ValueError:
            continue
    return procs


def snapshot(root=PROC):
    """The process table for this platform. Take a fresh one per refresh."""
    if root != PROC or os.path.isdir(os.path.join(PROC, "self")):
        return ProcTable(root)
    return PsTable()


def ancestors(pid, table, limit=64):
    """pid's parent chain, nearest first (excluding pid itself)."""
    out = []
    seen = {pid}
    for _ in range(limit):
        st = table.info(pid)
        if not st or st[0] <= 1 or st[0] in seen:
            break
        pid = st[0]
        seen.add(pid)
        out.append(pid)
    return out


def processes_named(name, proc=PROC, ps=None):
    """[(pid, argv)] of processes whose executable is called `name`.

    Linux: /proc/<pid>/comm, then the command line. Elsewhere: one `ps` listing, matching the
    basename of the first word (paths with spaces in them are not expected here).
    """
    out = []
    if ps is None and os.path.isdir(os.path.join(proc, "self")):
        try:
            names = os.listdir(proc)
        except OSError:
            return out
        for p in names:
            if not p.isdigit():
                continue
            try:
                with open(os.path.join(proc, p, "comm")) as f:
                    if f.read().strip() != name:
                        continue
                with open(os.path.join(proc, p, "cmdline"), "rb") as f:
                    argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
            except OSError:
                continue
            out.append((int(p), argv))
        return out
    if ps is None:
        try:
            ps = subprocess.run(["ps", "-A", "-o", "pid=,args="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return out
    for line in ps.splitlines():
        parts = line.split()
        if len(parts) >= 2 and os.path.basename(parts[1]) == name:
            try:
                out.append((int(parts[0]), parts[1:]))
            except ValueError:
                continue
    return out


def start_epoch(token, proc=PROC):
    """A start token (see the module docstring) as epoch seconds, or 0.0."""
    token = str(token or "").strip()
    if token.isdigit():  # Linux clock ticks since boot
        try:
            with open(os.path.join(proc, "stat")) as f:
                btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
            return btime + int(token) / os.sysconf("SC_CLK_TCK")
        except (OSError, StopIteration, ValueError):
            return 0.0
    try:  # macOS lstart, which we read in UTC
        return float(calendar.timegm(time.strptime(" ".join(token.split()), "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return 0.0


def pid_domain(proc=PROC, machine_id_path="/etc/machine-id"):
    """Claude's pidDomain on Linux: "linux:<machine-id>:pid:[<ns inode>]".

    Elsewhere Claude writes a constant, so there's nothing to compare against: None.
    """
    if not sys.platform.startswith("linux") and proc == PROC:
        return None
    try:
        with open(machine_id_path) as f:
            mid = f.read().strip()
        ns = os.readlink(os.path.join(proc, "self", "ns", "pid"))
    except OSError:
        return None
    return f"linux:{mid}:{ns}"
