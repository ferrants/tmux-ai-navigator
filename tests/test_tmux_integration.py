"""End-to-end against a private tmux server (-L), never the user's own server.

Starts a fake "agent" process inside a pane, writes a matching Claude session file, and
checks discovery, the picker window toggle, and switching an attached client to the agent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from tmux_ai_navigator import proc  # noqa: E402
from tmux_ai_navigator.cli import main  # noqa: E402
from tmux_ai_navigator.tmux import Tmux  # noqa: E402

SOCK = f"ainav-test-{os.getpid()}"


def pty_command(cmd):
    """Run cmd under a pseudo-terminal (util-linux and BSD `script` differ)."""
    if sys.platform == "darwin":
        return ["script", "-q", "/dev/null", "sh", "-c", cmd]
    return ["script", "-qfc", cmd, "/dev/null"]


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return None


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TmuxIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = Tmux(socket_name=SOCK)
        # keep seen/later state away from the user's real state dir (also for picker windows,
        # which inherit the test server's environment)
        cls.state_home = tempfile.mkdtemp()
        os.environ["XDG_STATE_HOME"] = cls.state_home
        os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp()
        env = dict(os.environ)
        env.pop("TMUX", None)
        cls.env = env
        subprocess.run(["tmux", "-L", SOCK, "-f", "/dev/null", "new-session", "-d", "-s", "home",
                        "-x", "120", "-y", "40"], check=True, env=env)
        subprocess.run(["tmux", "-L", SOCK, "new-session", "-d", "-s", "work", "-n", "agentwin"],
                       check=True, env=env)
        # the "agent": a child of the pane's shell, like claude under bash
        cls.t.run("send-keys", "-t", "work:agentwin", "exec sleep 600 & wait", "Enter")
        cls.pane = cls.t.run("display", "-p", "-t", "work:agentwin", "#{pane_id}").strip()
        shell = int(cls.t.run("display", "-p", "-t", cls.pane, "#{pane_pid}").strip())

        def child():
            out = subprocess.run(["pgrep", "-P", str(shell), "sleep"], capture_output=True, text=True).stdout
            return int(out.split()[0]) if out.split() else None
        cls.agent_pid = wait_for(child)
        assert cls.agent_pid, "fake agent did not start"
        cls.sessions = tempfile.mkdtemp()
        start = proc.snapshot().info(cls.agent_pid)[1]
        with open(os.path.join(cls.sessions, f"{cls.agent_pid}.json"), "w") as f:
            json.dump({"pid": cls.agent_pid, "sessionId": "test-session", "cwd": "/tmp", "name": "fake-agent",
                       "status": "idle", "statusUpdatedAt": int(time.time() * 1000) - 120000,
                       "startedAt": int(time.time() * 1000) - 7200000,
                       "procStart": start, "pidDomain": proc.pid_domain(), "kind": "interactive"}, f)
        # stale file: dead pid
        with open(os.path.join(cls.sessions, "999999.json"), "w") as f:
            json.dump({"pid": 999999, "sessionId": "dead", "kind": "interactive"}, f)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)

    def run_cli(self, *args):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["-L", SOCK, "--sessions-dir", self.sessions, *args])
        return rc, buf.getvalue()

    def test_list_finds_agent_in_its_pane(self):
        rc, out = self.run_cli("list", "--json")
        self.assertEqual(rc, 0)
        agents = json.loads(out)
        self.assertEqual(len(agents), 1)
        a = agents[0]
        self.assertEqual((a["name"], a["pane_id"], a["tmux_session"], a["window_name"]),
                         ("fake-agent", self.pane, "work", "agentwin"))
        index = self.t.run("display", "-p", "-t", self.pane, "#{window_index}").strip()
        self.assertEqual(a["location"], f"work:{index}")

    def test_open_toggles_picker_window(self):
        home_pane = self.t.run("display", "-p", "-t", "home:", "#{pane_id}").strip()
        self.assertEqual(self.run_cli("open", "--pane", home_pane)[0], 0)
        wins = self.t.run("list-windows", "-t", "home", "-F", "#{window_id} #{window_name} #{@ainav_window}")
        picker = [l.split()[0] for l in wins.splitlines() if l.endswith(" 1")]
        self.assertEqual(len(picker), 1, wins)
        # opening again from another pane replaces rather than duplicates
        self.run_cli("open", "--pane", home_pane)
        wins = self.t.run("list-windows", "-t", "home", "-F", "#{window_id} #{@ainav_window}")
        self.assertEqual(sum(1 for l in wins.splitlines() if l.endswith(" 1")), 1)
        picker = [l.split()[0] for l in wins.splitlines() if l.endswith(" 1")][0]
        # pressing the key inside the picker closes it
        ppane = self.t.run("display", "-p", "-t", picker, "#{pane_id}").strip()
        self.run_cli("open", "--pane", ppane)
        wins = self.t.run("list-windows", "-t", "home", "-F", "#{@ainav_window}")
        self.assertNotIn("1", wins.split())

    def state(self):
        from tmux_ai_navigator.state import State
        return State(os.path.join(self.state_home, "tmux-ai-navigator"))

    def agent(self):
        return json.loads(self.run_cli("list", "--json")[1])[0]

    def test_done_until_seen(self):
        with self.state().update() as d:   # first use an hour ago: the agent finished after that
            d["created"] = time.time() - 3600
            d["seen"], d["visible"], d.pop("server", None)
        self.assertTrue(self.agent()["done"])
        client = self.attach("work")
        try:
            wait_for(lambda: self.t.clients())
            self.t.run("select-window", "-t", "work:agentwin")
            self.assertEqual(self.run_cli("seen")[0], 0)
            self.assertFalse(self.agent()["done"])          # on screen now
            self.t.run("new-window", "-t", "work:")
            self.run_cli("seen")                             # left it: seen when it left the screen
            self.assertFalse(self.agent()["done"])
        finally:
            client.terminate()
            client.wait(timeout=5)
            client.stdin.close()

    def test_plugin_hooks_record_seen(self):
        sock = self.t.run("display", "-p", "#{socket_path}").strip()
        env = dict(self.env, TMUX=f"{sock},0,0")
        subprocess.run(["bash", os.path.join(HERE, "..", "tmux-ai-navigator.tmux")], env=env, check=True)
        hooks = self.t.run("show-hooks", "-g")
        self.assertIn("after-select-window[87]", hooks)
        self.assertIn("client-detached[87]", hooks)
        subprocess.run(["bash", os.path.join(HERE, "..", "tmux-ai-navigator.tmux")], env=env, check=True)
        self.assertEqual(self.t.run("show-hooks", "-g").count("after-select-window["), 1)  # no duplicates
        before = self.state().read()["seen"].get(self.pane, 0)
        client = self.attach("work")
        try:
            wait_for(lambda: self.t.clients())
            self.t.run("new-window", "-t", "work:")
            self.t.run("select-window", "-t", "work:agentwin")
            got = wait_for(lambda: self.state().read()["seen"].get(self.pane, 0) > before, timeout=8)
            self.assertTrue(got, "hook did not record the pane as seen")
        finally:
            client.terminate()
            client.wait(timeout=5)
            client.stdin.close()

    def test_open_uses_clients_session_for_linked_window(self):
        # link work's agent window into "home" too, then open from that pane as a client of "home"
        self.t.run("link-window", "-s", "work:agentwin", "-t", "home:")
        client = self.attach("home")
        try:
            tty = wait_for(lambda: (self.t.clients() or [None])[0])
            self.t.run("select-window", "-t", "home:agentwin")
            self.run_cli("open", "--client", tty, "--pane", self.pane)
            wins = self.t.run("list-windows", "-a", "-F", "#{session_name} #{@ainav_window}")
            self.assertIn("home 1", wins.splitlines())
            self.assertNotIn("work 1", wins.splitlines())
        finally:
            for line in self.t.run("list-windows", "-a", "-F", "#{window_id} #{@ainav_window}").splitlines():
                if line.endswith(" 1"):
                    self.t.run("kill-window", "-t", line.split()[0], check=False)
            self.t.run("unlink-window", "-t", "home:agentwin", check=False)
            client.terminate()
            client.wait(timeout=5)
            client.stdin.close()

    def set_status(self, status):
        path = os.path.join(self.sessions, f"{self.agent_pid}.json")
        with open(path) as f:
            d = json.load(f)
        d.update(status=status, statusUpdatedAt=int(time.time() * 1000))
        with open(path + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(path + ".tmp", path)

    def test_watcher_notifies_and_restarts(self):
        log = os.path.join(tempfile.mkdtemp(), "events")
        state_dir = tempfile.mkdtemp()
        for k, v in (("@ainav-notify", "none"), ("@ainav-notify-delay", "0"),
                     ("@ainav-notify-command", f'echo "$AINAV_EVENT $AINAV_SESSION_ID" >> {log}')):
            self.t.run("set", "-g", k, v)
        bin_ = os.path.join(HERE, "..", "bin", "tmux-ai-nav")
        start = [bin_, "-L", SOCK, "--sessions-dir", self.sessions, "--state-dir", state_dir, "watch", "--start"]
        server = self.t.server_pid()
        lock = os.path.join(state_dir, f"watch-{server}.lock")

        def watcher_pid():
            try:
                with open(lock) as f:
                    pid = int(f.read() or 0)
                os.kill(pid, 0)
                return pid
            except (OSError, ValueError):
                return None
        try:
            subprocess.run(start, check=True, env=self.env)
            first = wait_for(watcher_pid)
            self.assertTrue(first, "watcher did not start")
            subprocess.run(start, check=True, env=self.env)  # re-sourcing the config: replaced, not doubled
            second = wait_for(lambda: (watcher_pid() or first) != first and watcher_pid())
            self.assertTrue(second, "watcher was not replaced")
            self.assertFalse(wait_for(lambda: not os.path.exists(f"/proc/{first}") or
                                      subprocess.run(["kill", "-0", str(first)], capture_output=True).returncode,
                                      timeout=3) is None)
            time.sleep(1.0)  # let it take its first snapshot
            self.set_status("busy")
            time.sleep(1.0)
            self.set_status("idle")
            got = wait_for(lambda: os.path.exists(log) and open(log).read().strip(), timeout=6)
            self.assertEqual(got, "done test-session")
            self.set_status("waiting")
            got = wait_for(lambda: open(log).read().count("\n") >= 2 and open(log).read(), timeout=6)
            self.assertIn("waiting test-session", got or "")
        finally:
            pid = watcher_pid()
            if pid:
                os.kill(pid, 15)
            self.set_status("idle")
            for k in ("@ainav-notify", "@ainav-notify-delay", "@ainav-notify-command"):
                self.t.run("set", "-gu", k, check=False)

    def attach(self, session):
        return subprocess.Popen(pty_command(f"tmux -L {SOCK} attach -t {session}"),
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(self.env, TERM="xterm-256color"))

    def test_jump_switches_attached_client(self):
        # attach a real client to "home" through a pty
        client = subprocess.Popen(pty_command(f"tmux -L {SOCK} attach -t home"),
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env=dict(self.env, TERM="xterm-256color"))
        try:
            tty = wait_for(lambda: (self.t.clients() or [None])[0])
            self.assertTrue(tty, "client did not attach")
            # give the target session a different current window and pane, so the jump must change both
            self.t.run("split-window", "-t", self.pane)
            self.t.run("new-window", "-t", "work:")
            sid, wid = self.t.run("display", "-p", "-t", self.pane, "#{session_id} #{window_id}").split()
            self.t.jump(tty, sid, wid, self.pane)
            now = wait_for(lambda: self.t.run("display", "-p", "-c", tty, "#{session_name} #{pane_id}").strip()
                           == f"work {self.pane}")
            self.assertTrue(now, self.t.run("display", "-p", "-c", tty, "#{session_name} #{pane_id}"))
        finally:
            client.terminate()
            client.wait(timeout=5)
            client.stdin.close()


if __name__ == "__main__":
    unittest.main()
