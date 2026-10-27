import json
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tmux_ai_navigator import agents, claude, codex, extras, markdown, notify, proc, state, transcript, views  # noqa: E402
from tmux_ai_navigator.agents import Agent, arrange, clean_title, fmt_age, map_to_panes, pick_title, sort_agents  # noqa: E402
from tmux_ai_navigator.tmux import Pane  # noqa: E402
from tmux_ai_navigator.ui import Picker, build_items, fit, fit_left, matches, width  # noqa: E402


def write_stat(root, pid, ppid, start, comm="claude"):
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    # fields: pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt
    #         utime stime cutime cstime priority nice threads itrealvalue starttime ...
    fields = ["S", ppid] + [0] * 17 + [start, 0, 0]
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{pid} ({comm}) " + " ".join(str(x) for x in fields) + "\n")


def pane(pid, pane_id="%1", session="$1", name="main", window="@1", index=1, wname="win"):
    return Pane(pane_id, pid, session, name, window, index, wname, True, True, "/tmp")


class ProcTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_read_stat_handles_spaces_and_parens_in_comm(self):
        write_stat(self.root, 50, 7, 12345, comm="weird ) name (x")
        self.assertEqual(proc.read_stat(50, self.root), (7, 12345))

    def test_read_stat_missing(self):
        self.assertIsNone(proc.read_stat(999, self.root))

    def test_ancestors(self):
        write_stat(self.root, 10, 1, 1, "bash")
        write_stat(self.root, 20, 10, 2, "node")
        write_stat(self.root, 30, 20, 3)
        self.assertEqual(proc.ancestors(30, proc.ProcTable(self.root)), [20, 10])

    def test_ancestors_cycle_guard(self):
        write_stat(self.root, 10, 20, 1)
        write_stat(self.root, 20, 10, 1)
        self.assertEqual(proc.ancestors(10, proc.ProcTable(self.root)), [20])

    def test_ps_table_macos_format(self):
        out = ("    1     0 Mon Oct  6 09:00:01 2026\n"
               "  412     1 Mon Oct 13 18:49:01 2026\n"
               "  990   412 Tue Oct 14 08:05:59 2026\n"
               "garbage line\n")
        t = proc.PsTable(out)
        self.assertEqual(t.info(412), (1, "Mon Oct 13 18:49:01 2026"))
        self.assertEqual(t.info(1), (0, "Mon Oct  6 09:00:01 2026"))  # inner spacing kept
        self.assertIsNone(t.info(5))
        self.assertEqual(proc.ancestors(990, t), [412])

    def test_ps_snapshot_matches_live_session(self):
        t = proc.PsTable("  100     1 Mon Oct 13 18:49:01 2026\n")
        entry = {"pid": 100, "sessionId": "s", "procStart": "Mon Oct 13 18:49:01 2026", "pidDomain": "linux"}
        self.assertEqual(len(claude.live([entry], t, None)), 1)
        self.assertEqual(claude.live([dict(entry, procStart="Mon Oct 13 18:49:02 2026")], t, None), [])


class ShellTests(unittest.TestCase):
    SNAP = ("source /home/u/.claude/shell-snapshots/snapshot-bash-1-x.sh 2>/dev/null || true && shopt -u extglob "
            "2>/dev/null || true && eval 'cd /repo && npm run dev -- --port '\"'\"'3000'\"'\"'' < /dev/null "
            "&& pwd -P >| /tmp/claude-1a2b-cwd")

    def test_shell_command(self):
        self.assertEqual(proc.shell_command(["/bin/bash", "-c", self.SNAP]), "cd /repo && npm run dev -- --port '3000'")
        self.assertEqual(proc.shell_command(["/bin/zsh", "-c", "-l", "shopt -u extglob 2>/dev/null && eval 'make' "
                                             "< /dev/null && pwd -P >| /tmp/c"]), "make")
        self.assertIsNone(proc.shell_command(["/bin/sh", "-c", "statusline.sh"]))  # hooks, status line
        self.assertIsNone(proc.shell_command(["/usr/bin/python3", "-c", self.SNAP]))
        self.assertIsNone(proc.shell_command([]))
        # macOS ps joins the command line with spaces
        self.assertEqual(proc.shell_command(["/bin/bash", "-c " + self.SNAP]), "cd /repo && npm run dev -- --port '3000'")

    def test_background_shells_and_busy_turns(self):
        class Table:
            procs = {10: (1, "a"), 11: (10, "b"), 12: (10, "c"), 13: (10, "d")}
            argvs = {11: ["/bin/bash", "-c", ShellTests.SNAP], 12: ["node", "mcp.js"],
                    13: ["/bin/bash", "-c", ShellTests.SNAP.replace("npm run dev", "pytest")]}

            def info(self, pid):
                return self.procs.get(pid)

            def children(self, pid):
                return [p for p, (pp, _) in self.procs.items() if pp == pid]

            def argv(self, pid):
                return Table.argvs[pid]
        starts = {"b": 100.0, "d": 500.0}
        with mock.patch.object(proc, "start_epoch", lambda tok, proc=None: starts.get(tok, 0.0)):
            a = ag("x", "idle", since=400.0)
            a.pid = 10
            self.assertEqual([s["pid"] for s in agents.background_shells(a, Table())], [11, 13])
            a.status = "busy"  # pytest started this turn: maybe the command it's running right now
            self.assertEqual([s["pid"] for s in agents.background_shells(a, Table())], [11])
        a = ag("y", "shell", since=50.0)
        a.pid = 99
        self.assertEqual(agents.background_shells(a, Table()), [{"pid": None, "started": 50.0, "command": ""}])

    def test_short_command(self):
        from tmux_ai_navigator.ui import short_command
        self.assertEqual(short_command("cd /repo && S=/tmp/x; npm test"), "npm test")
        self.assertEqual(short_command("cd /repo\nS=/tmp/x\nfor i in 1 2; do sleep 1; done"), "for i in 1 2; do sleep 1; done")
        self.assertEqual(short_command("cd /repo"), "cd /repo")
        self.assertEqual(short_command(""), "")

    def test_proc_table_children_and_argv(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "10", "task", "10"))
        with open(os.path.join(root, "10", "task", "10", "children"), "w") as f:
            f.write("11 12 ")
        with open(os.path.join(root, "10", "cmdline"), "wb") as f:
            f.write(b"/bin/bash\0-c\0echo hi\0")
        t = proc.ProcTable(root)
        self.assertEqual(t.children(10), [11, 12])
        self.assertEqual(t.argv(10), ["/bin/bash", "-c", "echo hi"])
        self.assertEqual((t.children(99), t.argv(99)), ([], []))
        ps = proc.PsTable(output="", args={10: (1, "/bin/bash -c echo hi"), 11: (10, "sleep 5")})
        self.assertEqual((ps.children(10), ps.argv(10)), ([11], ["/bin/bash", "-c echo hi"]))


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        write_stat(self.root, 100, 1, 5000)

    def entry(self, **kw):
        d = {"pid": 100, "sessionId": "s1", "procStart": "5000", "kind": "interactive",
             "pidDomain": "dom", "updatedAt": 1}
        d.update(kw)
        return d

    def test_live_entry_kept(self):
        self.assertEqual(len(claude.live([self.entry()], proc.ProcTable(self.root), "dom")), 1)

    def test_dead_pid_dropped(self):
        self.assertEqual(claude.live([self.entry(pid=101)], proc.ProcTable(self.root), "dom"), [])

    def test_reused_pid_dropped(self):
        self.assertEqual(claude.live([self.entry(procStart="4999")], proc.ProcTable(self.root), "dom"), [])

    def test_other_pid_domain_dropped(self):
        self.assertEqual(claude.live([self.entry(pidDomain="other")], proc.ProcTable(self.root), "dom"), [])

    def test_non_interactive_and_malformed_dropped(self):
        bad = [self.entry(kind="sdk"), {"pid": "100", "sessionId": "x"}, {"sessionId": "y"}]
        self.assertEqual(claude.live(bad, proc.ProcTable(self.root), "dom"), [])

    def test_duplicate_keeps_newest(self):
        out = claude.live([self.entry(updatedAt=1, name="old"), self.entry(updatedAt=2, name="new")],
                          proc.ProcTable(self.root), "dom")
        self.assertEqual([d["name"] for d in out], ["new"])

    def test_session_files_cache_and_bad_json(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "1.json"), "w") as f:
            json.dump({"pid": 1}, f)
        with open(os.path.join(d, "2.json"), "w") as f:
            f.write("{not json")
        sf = claude.SessionFiles(d)
        self.assertEqual(sf.read(), [{"pid": 1}])
        with open(os.path.join(d, "1.json"), "w") as f:
            f.write('{"pid": 1, "sta')  # mid-write: the last good copy stands in
        os.utime(os.path.join(d, "1.json"), ns=(1, 1))
        self.assertEqual(sf.read(), [{"pid": 1}])
        with open(os.path.join(d, "1.json"), "w") as f:
            json.dump({"pid": 1, "status": "idle"}, f)
        os.utime(os.path.join(d, "1.json"), ns=(2, 2))
        self.assertEqual(sf.read(), [{"pid": 1, "status": "idle"}])
        os.remove(os.path.join(d, "1.json"))
        self.assertEqual(sf.read(), [])


class FakeTmux:
    def panes(self):
        return []

    def server_pid(self):
        return 1


class DiscoveryTests(unittest.TestCase):
    def test_malformed_fields_are_treated_as_missing(self):
        root, sessions = tempfile.mkdtemp(), tempfile.mkdtemp()
        write_stat(root, 100, 1, 5000)
        with open(os.path.join(sessions, "100.json"), "w") as f:
            json.dump({"pid": 100, "sessionId": "s1", "procStart": "5000", "cwd": {"x": 1}, "name": 5,
                       "status": None, "statusUpdatedAt": "soon", "startedAt": [1]}, f)
        d = agents.Discovery(FakeTmux(), sessions, proc=root, state_store=state.State(tempfile.mkdtemp()))
        got = d.agents(include_untracked=True, record_seen=False)
        self.assertEqual([(a.session_id, a.cwd, a.status, a.since) for a in got], [("s1", "", "unknown", None)])


class MappingTests(unittest.TestCase):
    def test_maps_through_process_tree(self):
        root = tempfile.mkdtemp()
        write_stat(root, 10, 1, 1, "bash")      # pane shell
        write_stat(root, 20, 10, 1, "sh")       # wrapper
        write_stat(root, 30, 20, 1)             # claude
        write_stat(root, 40, 1, 1)              # claude outside tmux
        m = map_to_panes([30, 40], [pane(10)], proc.ProcTable(root))
        self.assertEqual(m[30][0].pane_id, "%1")
        self.assertNotIn(40, m)

    def test_linked_window_prefers_current_session(self):
        root = tempfile.mkdtemp()
        write_stat(root, 30, 10, 1)
        panes = [pane(10, session="$1", name="a"), pane(10, session="$2", name="b")]
        self.assertEqual(map_to_panes([30], panes, proc.ProcTable(root), current_session="$2")[30][0].session_name, "b")


class ExtrasTests(unittest.TestCase):
    def test_git_branch_and_worktree(self):
        repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/feature/x\n")
        sub = os.path.join(repo, "a", "b")
        os.makedirs(sub)
        self.assertEqual(agents.git_branch(sub), "feature/x")
        wt = tempfile.mkdtemp()
        gd = os.path.join(repo, ".git", "worktrees", "wt")
        os.makedirs(gd)
        with open(os.path.join(gd, "HEAD"), "w") as f:
            f.write("0123456789abcdef\n")
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write(f"gitdir: {gd}\n")
        self.assertEqual(agents.git_branch(wt), "01234567")

    def test_extras_command_protocol(self):
        calls = []

        def runner(cmd, stdin):
            calls.append(json.loads(stdin))
            return json.dumps({"s1": {"badge": "85%", "badge_style": "warn", "title": "Ship it",
                                      "lines": ["Goal: ship it", 3], "search": "kiwi"},
                               "s2": "not a dict"})

        ex = extras.Extras("cmd", interval=5, runner=runner)
        a1, a2 = ag("s1"), ag("s2")
        data = ex.get([a1, a2], now=100)
        self.assertEqual(calls[0]["sessions"][0]["session_id"], "s1")
        self.assertNotIn("s2", data)
        extras.apply(a1, data["s1"])
        self.assertEqual((a1.badge, a1.badge_style, a1.extra_title, a1.extra_lines),
                         ("85%", "warn", "Ship it", ["Goal: ship it", "3"]))
        self.assertIn("kiwi", a1.search_text())
        ex.get([a1, a2], now=101)               # fresh: not re-run
        ex.get([a1, a2, ag("s3")], now=102)     # new session: re-run
        ex.get([a1], now=200)                   # stale: re-run
        self.assertEqual(len(calls), 3)

    def test_extras_failure_is_reported_not_raised(self):
        def runner(cmd, stdin):
            raise RuntimeError("boom")
        ex = extras.Extras("cmd", runner=runner)
        self.assertEqual(ex.get([ag("s1")]), {})
        self.assertIn("boom", ex.error)
        self.assertEqual(extras.Extras(None).get([ag("s1")]), {})

    def test_extras_real_command(self):
        ex = extras.Extras("""python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({s["session_id"]: {"badge": "ok"} for s in d["sessions"]}))'""")
        self.assertEqual(ex.get([ag("s1")]), {"s1": {"badge": "ok"}})
        ex = extras.Extras("exit 3")
        ex.get([ag("s1")])
        self.assertIn("exit 3", ex.error)

    def test_extras_timeout_kills_the_whole_command(self):
        marker = tempfile.mktemp()
        old, extras.TIMEOUT = extras.TIMEOUT, 0.5
        try:
            ex = extras.Extras(f"(sleep 1.5; touch {marker}) & sleep 5")
            start = time.time()
            ex.get([ag("s1")])
            self.assertLess(time.time() - start, 3)
            self.assertIn("timed out", ex.error)
            time.sleep(1.5)
            self.assertFalse(os.path.exists(marker), "background child survived the timeout")
        finally:
            extras.TIMEOUT = old


def ag(name, status="idle", since=0.0, session="s", index=1, **kw):
    kw.setdefault("title", name)
    kw.setdefault("cwd", "/x")
    return Agent(kind="claude", pid=hash(name) & 0xffff, session_id=name, name=name,
                 status=status, since=since, tmux_session=session, window_index=index,
                 pane_id="%" + name, tmux_session_id=kw.pop("sid", None), **kw)


class SortSearchTests(unittest.TestCase):
    NOW = 10 * 86400.0

    def test_sections_and_order(self):
        now = self.NOW
        a = [ag("old", since=now - 3 * 86400), ag("recent", since=now - 600), ag("older-today", since=now - 7200),
             ag("ask", "waiting", since=now - 50), ag("fin", since=now - 30, done=True),
             ag("flag", since=now - 9000, later=True), ag("run", "busy", since=now - 5)]
        got = [(k, [x.name for x in items]) for k, _, items in arrange(a, now=now)]
        self.assertEqual(got, [("needs", ["ask", "fin", "flag"]), ("working", ["run"]),
                               ("today", ["recent", "older-today"]), ("older", ["old"])])

    def test_pinned_first_by_recent_activity_in_both_modes(self):
        now = self.NOW
        a = [ag("ask", "waiting", since=now - 50), ag("pin-old", since=now - 3 * 86400, pinned=True),
             ag("pin-ask", "waiting", since=now - 500, session="api", pinned=True),
             ag("pin-run", "busy", since=now - 5, pinned=True), ag("idle", since=now - 60)]
        got = [(k, [x.name for x in items]) for k, _, items in arrange(a, now=now)]
        self.assertEqual(got, [("pinned", ["pin-run", "pin-ask", "pin-old"]), ("needs", ["ask"]),
                               ("today", ["idle"])])
        got = [(t, [x.name for x in items]) for _, t, items in arrange(a, "project", now)]
        self.assertEqual(got, [("PINNED", ["pin-run", "pin-ask", "pin-old"]), ("S", ["ask", "idle"])])
        self.assertEqual(a[2].section(now), "needs")  # still counts as needing you
        items, _ = build_items(a, "pinned", views.ALL, "priority", False, now)
        self.assertEqual([it[1].name for it in items if it[0] == "agent"], ["pin-run", "pin-ask", "pin-old"])

    def test_loops_are_searchable(self):
        a = ag("w", loops=[{"kind": "cron", "schedule": "Every 5 minutes", "prompt": "/check-deploy", "next": None}])
        self.assertTrue(matches(a, "loop") and matches(a, "check-deploy"))
        self.assertFalse(matches(ag("v"), "loop"))

    def test_project_mode_groups_by_tmux_session_tag(self):
        now = self.NOW
        a = [ag("a", session="1web", since=now - 9), ag("b", session="api", since=now - 1),
             ag("c", session="1web", since=now - 2)]
        got = [(t, [x.name for x in items]) for _, t, items in arrange(a, "project", now)]
        self.assertEqual(got, [("API", ["b"]), ("WEB", ["c", "a"])])

    def test_session_sort(self):
        a = [ag("b2", session="b", index=2), ag("a1", session="a", index=1), ag("b1", session="b", index=1)]
        self.assertEqual([x.name for x in sort_agents(a, "session")], ["a1", "b1", "b2"])

    def test_older_folds_unless_expanded_or_searching(self):
        now = self.NOW
        a = [ag("old", since=now - 3 * 86400), ag("new", since=now - 60)]
        items, _ = build_items(a, "", None, "priority", False, now)
        self.assertEqual([i[0] for i in items], ["header", "agent", "header", "more"])
        items, _ = build_items(a, "", None, "priority", True, now)
        self.assertEqual([i[0] for i in items], ["header", "agent", "header", "agent"])
        items, _ = build_items(a, "old", None, "priority", False, now)
        self.assertEqual([i[0] for i in items], ["header", "agent"])

    def test_scopes(self):
        now = self.NOW
        a = [ag("w", session="web", sid="$1", cwd="/r/web/src"), ag("i", session="infra", sid="$2", cwd="/r/infra"),
             ag("l", session="infra", sid="$2", cwd="/r/web", session_ids=["$2", "$1"])]  # linked into web too
        names = lambda sc: [i[1].name for i in build_items(a, "", sc, "priority", True, now)[0] if i[0] == "agent"]
        roots = {"/r/web/src": "/r/web", "/r/web": "/r/web", "/r/infra": "/r/infra"}
        with mock.patch.object(views, "project_root", side_effect=lambda p: roots.get(p, p)):
            self.assertEqual(sorted(names(None)), ["i", "l", "w"])
            self.assertEqual(sorted(names({"kind": "session", "value": "$1", "label": "web"})), ["l", "w"])
            self.assertEqual(names({"kind": "session", "value": "$2", "label": "infra"}), ["i", "l"])
            self.assertEqual(sorted(names({"kind": "project", "value": "/r/web", "label": "~/web"})), ["l", "w"])

    def test_matches_terms_and_smart_case(self):
        a = ag("Legacy video", branch="production")
        self.assertTrue(matches(a, "legacy prod"))
        self.assertFalse(matches(a, "legacy staging"))
        self.assertFalse(matches(a, "LEGACY"))
        self.assertTrue(matches(a, "Legacy"))

    def test_tag(self):
        self.assertEqual([ag("x", session=s).tag for s in ("1web", "4mobile", "api", "12")], ["web", "mobile", "api", "12"])

    def test_fmt_age(self):
        self.assertEqual([fmt_age(s) for s in (None, 30, 600, 3700, 40000, 200000)],
                         ["-", "<1m", "10m", "1h01m", "11h", "2d"])

    def test_fit_wide_chars(self):
        self.assertEqual(width(fit("日本語テキスト", 7)), 7)
        self.assertEqual(fit("abc", 5, pad=True), "abc  ")


class ViewTests(unittest.TestCase):
    NOW = 1_000_000.0

    def test_project_root(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "repo", ".git"))
        open(os.path.join(root, "repo", ".git", "HEAD"), "w").close()
        os.makedirs(os.path.join(root, "plain", ".git"))  # empty: not a repo
        os.makedirs(os.path.join(root, "repo", "src", "deep"))
        self.assertEqual(views.project_root(os.path.join(root, "repo", "src", "deep")), os.path.join(root, "repo"))
        self.assertEqual(views.project_root(os.path.join(root, "plain")), os.path.join(root, "plain"))
        self.assertEqual(views.project_root(os.path.join(root, "gone")), os.path.join(root, "gone"))
        self.assertEqual(views.project_root(""), "")
        os.makedirs(os.path.join(root, "repo", "wt2"))
        with open(os.path.join(root, "repo", "wt2", ".git"), "w") as f:
            f.write("gitdir: ../.git/worktrees/wt2\n")
        self.assertEqual(views.project_root(os.path.join(root, "repo", "wt2")), os.path.join(root, "repo", "wt2"))
        with mock.patch.dict(os.environ, {"HOME": root}):  # dotfiles repo at ~
            with open(os.path.join(root, ".git"), "w") as f:
                f.write("gitdir: x")
            os.makedirs(os.path.join(root, "notes"))
            self.assertEqual(views.project_root(os.path.join(root, "notes")), os.path.join(root, "notes"))

    def test_opener_scopes(self):
        with mock.patch.object(views, "project_root", side_effect=lambda p: p and "/x/api"):
            got = views.opener_scopes("$3", "2api", "/x/api/src")
        self.assertEqual([(s["kind"], s["value"], s["label"]) for s in got],
                         [("all", "", "all"), ("session", "$3", "api"), ("project", "/x/api", "/x/api")])
        self.assertEqual([s["kind"] for s in views.opener_scopes()], ["all"])

    def test_next_key(self):
        keys = [(1, "a"), (2, "b"), views.OLDER]
        self.assertEqual(views.next_key(keys, (1, "a")), (2, "b"))
        self.assertEqual(views.next_key(keys, (2, "b")), views.OLDER)
        self.assertEqual(views.next_key(keys[:2], (2, "b")), (1, "a"))
        self.assertIsNone(views.next_key([(1, "a")], (1, "a")))
        self.assertIsNone(views.next_key(keys, (9, "z")))

    def test_back_round_trip_through_json(self):
        scope = {"kind": "session", "value": "$1", "label": "web"}
        for nxt in [(7, "sess-7"), views.OLDER, None]:
            d = {}
            views.save_back(d, 42, views.snapshot(scope, "auth", "project", True, nxt, now=self.NOW))
            d = json.loads(json.dumps(d))
            got = views.load_back(d, 42, 600, now=self.NOW + 60)
            self.assertEqual(got, {"scope": scope, "query": "auth", "mode": "project", "show_older": True,
                                   "next": nxt})
            self.assertIn("seen", d["servers"]["42"])  # a usable server record for the hooks

    def test_back_expires_and_is_per_server(self):
        d = {}
        views.save_back(d, 42, views.snapshot(views.ALL, "", "priority", False, None, now=self.NOW))
        self.assertIsNotNone(views.load_back(d, 42, 600, now=self.NOW + 599))
        self.assertIsNone(views.load_back(d, 42, 600, now=self.NOW + 601))
        self.assertIsNone(views.load_back(d, 42, 600, now=self.NOW - 5))  # clock went back
        self.assertIsNone(views.load_back(d, 42, 0, now=self.NOW))       # resume off
        self.assertIsNone(views.load_back(d, 43, 600, now=self.NOW))

    def test_back_rejects_malformed(self):
        good = views.snapshot(views.ALL, "q", "priority", False, (1, "a"), now=self.NOW)
        bad = [None, [], {}, dict(good, saved_at="x"), dict(good, saved_at=float("nan")),
               dict(good, scope={"kind": "bogus", "value": "", "label": ""}), dict(good, scope="all"),
               dict(good, query=5), dict(good, mode="tree")]
        for v in bad:
            self.assertIsNone(views.load_back({"servers": {"1": {"back": v}}}, 1, 600, now=self.NOW), v)
        self.assertIsNone(views.load_back({"servers": {"1": "junk"}}, 1, 600, now=self.NOW))
        odd = dict(good, next={"agent": ["1", 2]}, show_older="yes")
        got = views.load_back({"servers": {"1": {"back": odd}}}, 1, 600, now=self.NOW)
        self.assertEqual((got["next"], got["show_older"]), (None, False))

    def test_previous_pane_bounces(self):
        d = {}
        for t, pane in enumerate(["%1", "%9", "%2"]):  # agent %1, a plain shell %9, agent %2
            state.record_focus(d, 7, {"/dev/pts/1": pane}, now=100 + t)
        agents = {"%1", "%2", "%3"}
        self.assertEqual(state.previous_pane(d, 7, "/dev/pts/1", "%2", agents), "%1")  # skips the shell
        state.record_focus(d, 7, {"/dev/pts/1": "%1"}, now=200)                       # jumped back
        self.assertEqual(state.previous_pane(d, 7, "/dev/pts/1", "%1", agents), "%2")  # and again
        self.assertEqual(d["servers"]["7"]["recent"]["/dev/pts/1"]["panes"], ["%1", "%2", "%9"])
        self.assertIsNone(state.previous_pane(d, 7, "/dev/pts/1", "%1", {"%1"}))
        # a client with no history of its own (a new terminal) borrows the others'
        self.assertEqual(state.previous_pane(d, 7, "/dev/pts/5", "%3", agents), "%1")
        self.assertFalse(state.focus_changed(d, 7, {"/dev/pts/1": "%1"}))
        self.assertTrue(state.focus_changed(d, 7, {"/dev/pts/1": "%2"}))
        self.assertTrue(state.focus_changed(d, 7, {"/dev/pts/2": "%2"}))

    def test_recent_is_bounded(self):
        d = {}
        for i in range(40):
            state.record_focus(d, 7, {f"/dev/pts/{i % 20}": f"%{i}"}, now=i)
        r = d["servers"]["7"]["recent"]
        self.assertEqual(len(r), state.RECENT_CLIENTS)
        self.assertNotIn("/dev/pts/0", r)  # least recently used terminals go first
        self.assertTrue(all(len(e["panes"]) <= state.RECENT_PANES for e in r.values()))
        bad = {"servers": {"7": {"recent": {"x": "junk", "y": {"panes": [3, "%1"]}}}}}
        self.assertEqual(state.previous_pane(bad, 7, "x", "%0", {"%1"}), "%1")
        state.record_focus(bad, 7, {"y": "%2"})
        self.assertEqual(bad["servers"]["7"]["recent"]["y"]["panes"], ["%2", "%1"])

    def test_worth_restoring(self):
        plain = {"scope": dict(views.ALL), "query": "", "mode": "priority", "show_older": False, "next": None}
        self.assertFalse(views.worth_restoring(plain))
        self.assertFalse(views.worth_restoring(None))
        for change in ({"query": "a"}, {"show_older": True}, {"mode": "project"},
                       {"scope": {"kind": "session", "value": "$1", "label": "web"}}):
            self.assertTrue(views.worth_restoring(dict(plain, **change)), change)

    def test_resume_window(self):
        self.assertEqual(views.resume_window(None), 600)
        self.assertEqual(views.resume_window("5"), 300)
        self.assertEqual(views.resume_window("0"), 0)
        for v in ("-1", "inf", "nan", "soon"):
            self.assertEqual(views.resume_window(v), 600)

    def test_picker_tabs(self):
        scopes = [dict(views.ALL), {"kind": "session", "value": "$1", "label": "web"},
                  {"kind": "project", "value": "/r/api", "label": "~/api"}]
        p = Picker(list, scopes=scopes, default_tab="session")
        self.assertEqual(p.tab.scope["kind"], "session")
        p.query = "x"
        p.tab_idx = 0
        self.assertEqual(p.query, "")  # each tab has its own search
        p.tab_idx = 1
        self.assertEqual(p.query, "x")
        self.assertEqual(Picker(list, scopes=scopes, default_tab="nope").tab.scope["kind"], "all")
        back = {"scope": scopes[1], "query": "auth", "mode": "project", "show_older": True, "next": (5, "s")}
        p = Picker(list, scopes=scopes, back=back)
        self.assertEqual((p.tab_idx, p.tab.back, p.query, p.sel_key, p.show_older, p.mode_idx),
                         (0, True, "auth", (5, "s"), True, 1))
        self.assertEqual(p.tab.label(), '↺ back · web · "auth"')
        older = dict(back, scope=dict(views.ALL), query="")
        self.assertEqual(Picker(list, scopes=scopes, back=older).tab.label(), "↺ back · older")
        self.assertEqual(Picker(list, scopes=scopes, back=dict(older, show_older=False, query="x")).tab.label(),
                         '↺ back · "x"')
        self.assertEqual(len(p.tabs), 4)

    def test_fit_left(self):
        self.assertEqual(fit_left("~/code/api", 20), "~/code/api")
        self.assertEqual(fit_left("~/code/very-long-project", 10), "…g-project")


class MarkdownTests(unittest.TestCase):
    def text(self, md, w):
        lines = [markdown.plain_text(l) for l in markdown.render(md, w)]
        for l in lines:
            self.assertLessEqual(markdown.width(l), w, l)
        return lines

    def test_inline(self):
        segs = markdown.inline("a **bold** and `code` and *em* and [link](http://x) and snake_case_name")
        self.assertEqual(segs, [("a ", "text"), ("bold", "bold"), (" and ", "text"), ("code", "code"),
                                (" and ", "text"), ("em", "em"), (" and ", "text"), ("link", "text"),
                                (" and snake_case_name", "text")])

    def test_table_aligns_and_wraps_cells(self):
        md = "| Name | Notes |\n|---|:--|\n| a | short |\n| longer name | " + "word " * 12 + "|"
        lines = self.text(md, 40)
        self.assertEqual(lines[0], "Name        │ Notes")
        self.assertTrue(lines[1].startswith("────────────┼─"))
        self.assertTrue(lines[2].startswith("a           │ short"))
        self.assertTrue(lines[3].startswith("────────────┼─"))     # rows wrap, so they're ruled
        self.assertTrue(lines[4].startswith("longer name │ word"))
        self.assertTrue(lines[5].startswith("            │ word"))  # wrapped inside its cell

    def test_table_too_wide_becomes_records(self):
        md = "| " + " | ".join(f"column {i}" for i in range(6)) + " |\n|" + "---|" * 6 + "\n| " + \
             " | ".join(f"value {i}" for i in range(6)) + " |"
        lines = self.text(md, 30)
        self.assertEqual(lines[:2], ["column 0: value 0", "column 1: value 1"])

    def test_blocks(self):
        md = "# Title\n\n- one\n  - nested item that wraps around\n2. two\n- [x] done\n> quote\n---\n```\ncode line\n```\n\n\n\nend"
        self.assertEqual(self.text(md, 20), ["Title", "", "• one", "  • nested item that", "    wraps around",
                                              "2. two", "☑ done", "│ quote", "─" * 20, "  code line", "", "end"])

    def test_long_words_and_wide_chars(self):
        for l in self.text("x" * 50 + " 日本語のテキスト" * 5, 12):
            self.assertTrue(l)

    def test_search_includes_preview_text(self):
        a = ag("x")
        items, _ = build_items([a], "redis", None, "priority", True, 0,
                               text=lambda _: "uses Redis for limits")
        self.assertEqual(len([i for i in items if i[0] == "agent"]), 1)
        items, _ = build_items([a], "redis", None, "priority", True, 0)
        self.assertEqual(items, [])


class TitleTests(unittest.TestCase):
    def test_clean_title_strips_status_glyph(self):
        self.assertEqual(clean_title("✳ Fix login bug", "host"), "Fix login bug")
        self.assertEqual(clean_title("⠂ (draft) notes", "host"), "(draft) notes")
        self.assertEqual(clean_title("host", "host"), "")
        self.assertEqual(clean_title("✳ Claude Code", "host"), "")

    def test_pick_title_fallbacks(self):
        a = ag("web-4c", pane_title=socket.gethostname(), window_name="t-cd7b06bc", title="")
        self.assertEqual(pick_title(a, titles=("", "")), "web-4c")
        a.extra_title = "ship it"
        self.assertEqual(pick_title(a, titles=("", "")), "ship it")
        self.assertEqual(pick_title(a, titles=("", "From transcript")), "From transcript")
        a.window_name = "landing-page"
        a.extra_title = ""
        self.assertEqual(pick_title(a, titles=("", "")), "landing-page")
        a.pane_title = "✳ Real title"
        self.assertEqual(pick_title(a, titles=("x", "y")), "Real title")

    def test_weak_pane_title_loses_to_ai_title(self):
        a = ag("t-12faa60a", pane_title="✳ t-12faa60a", window_name="t-f6b8a536", title="")
        self.assertEqual(pick_title(a, titles=("t-12faa60a", "Reprobe disabled reports")), "Reprobe disabled reports")
        a = ag("x", pane_title=" app", title="")
        a.cwd = "/code/app"
        self.assertEqual(pick_title(a, titles=("", "Fix the build")), "Fix the build")
        self.assertEqual(pick_title(a, titles=("", "")), "app")   # nothing better: keep it

    def test_shell_status_is_not_working(self):
        now = 10 * 86400.0
        self.assertEqual(ag("s", "shell", since=now - 9 * 3600).section(now), "today")


class StateTests(unittest.TestCase):
    def test_done_lifecycle(self):
        d = {"servers": {}, "later": {}}
        state.mark_visible(d, "srv", set(), now=100.0)          # first use
        v = state.server_view(d, "srv")
        a = ag("x", since=200.0, started=50.0)
        self.assertTrue(state.is_done(a, v, set()))            # finished after first use, never looked
        self.assertFalse(state.is_done(a, v, {"%x"}))          # on screen right now
        state.mark_visible(d, "srv", {"%x"}, now=300.0)
        state.mark_visible(d, "srv", set(), now=310.0)          # left the pane: seen at 310
        self.assertEqual(v["seen"]["%x"], 310.0)
        self.assertFalse(state.is_done(a, v, set()))
        a.since = 400.0                                         # finished again later
        self.assertTrue(state.is_done(a, v, set()))

    def test_not_done_cases(self):
        v = {"seen": {}, "visible": [], "created": 100.0}
        self.assertFalse(state.is_done(ag("old", since=90.0, started=10.0), v, set()))   # before first use
        self.assertFalse(state.is_done(ag("fresh", since=205.0, started=200.0), v, set()))  # just started
        self.assertFalse(state.is_done(ag("busy", "busy", since=300.0, started=10.0), v, set()))
        unknown = state.server_view({"servers": {}}, "nope")    # untracked server: nothing is done yet
        self.assertFalse(state.is_done(ag("x", since=200.0, started=10.0), unknown, set()))

    def test_servers_are_separate_and_seen_is_monotonic(self):
        d = {"servers": {}, "later": {}}
        state.mark_visible(d, "1", {"%1"}, now=50.0)
        state.mark_visible(d, "2", {"%2"}, now=100.0, alive=lambda s: True)
        self.assertEqual(state.server_view(d, "1")["seen"], {"%1": 50.0})   # not wiped by server 2
        self.assertEqual(state.server_view(d, "2")["seen"], {"%2": 100.0})
        state.mark_visible(d, "2", {"%2"}, now=90.0)          # an older hook finishing late
        self.assertEqual(state.server_view(d, "2")["seen"]["%2"], 100.0)
        state.mark_visible(d, "2", set(), now=120.0, alive=lambda s: False)  # server 1 is gone
        self.assertEqual(list(d["servers"]), ["2"])

    def test_old_single_server_layout_is_migrated(self):
        st = state.State(tempfile.mkdtemp())
        os.makedirs(st.dir, exist_ok=True)
        with open(st.path, "w") as f:
            json.dump({"seen": {"%1": 5.0}, "visible": ["%1"], "server": "77", "created": 1.0, "later": {"s": 2.0}}, f)
        d = st.read()
        self.assertEqual(d["servers"], {"77": {"seen": {"%1": 5.0}, "visible": ["%1"], "created": 1.0}})
        self.assertEqual(d["later"], {"s": 2.0})
        self.assertNotIn("seen", d)

    def test_later_clears_after_next_status_change(self):
        d = {"seen": {}, "later": {}, "visible": []}
        a = ag("x", since=100.0)
        self.assertTrue(state.toggle_later(d, "x", now=200.0))
        self.assertTrue(state.later_active(a, d))
        self.assertFalse(state.toggle_later(d, "x", since=a.since))   # active flag: one press unflags
        self.assertTrue(state.toggle_later(d, "x", since=a.since, now=200.0))
        a.since = 250.0                                         # prompted it again
        self.assertFalse(state.later_active(a, d))
        self.assertTrue(state.toggle_later(d, "x", since=a.since, now=300.0))  # expired: one press re-flags
        self.assertTrue(state.later_active(a, d))

    def test_pins_toggle_persist_and_are_bounded(self):
        st = state.State(tempfile.mkdtemp())
        self.assertEqual(st.read()["pins"], {})
        with st.update() as d:
            self.assertTrue(state.toggle_pin(d, "s1", now=5.0))
        self.assertEqual(st.read()["pins"], {"s1": 5.0})
        with st.update() as d:
            self.assertFalse(state.toggle_pin(d, "s1"))
        self.assertEqual(st.read()["pins"], {})
        d = {"pins": {}}
        for i in range(state.MAX_PINS + 5):
            state.toggle_pin(d, f"s{i}", now=float(i + 1))
        self.assertEqual(len(d["pins"]), state.MAX_PINS)
        self.assertNotIn("s0", d["pins"])
        self.assertIn(f"s{state.MAX_PINS + 4}", d["pins"])

    def test_store_roundtrip(self):
        st = state.State(tempfile.mkdtemp())
        with st.update() as d:
            state.toggle_later(d, "s1", now=5.0)
        self.assertEqual(st.read()["later"], {"s1": 5.0})
        self.assertEqual(st.read()["servers"], {})


class TranscriptTests(unittest.TestCase):
    def write_transcript(self, lines, sid="sid"):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "projects", "-x"))
        path = os.path.join(home, "projects", "-x", f"{sid}.jsonl")
        with open(path, "w") as f:
            for l in lines:
                f.write(json.dumps(l, separators=(",", ":")) + "\n")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home})
        patcher.start()
        self.addCleanup(patcher.stop)
        transcript._paths.clear()
        return path

    @staticmethod
    def call(i, name, inp, ts, result=None, text="", error=False):
        """A tool_use line and its tool_result line, as Claude writes them."""
        use = {"type": "assistant", "timestamp": ts,
               "message": {"content": [{"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}]}}
        res = {"type": "user", "timestamp": ts, "toolUseResult": result,
               "message": {"content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": text,
                                        **({"is_error": True} if error else {})}]}}
        return [use, res]

    def test_loops_replay_create_delete_list_and_wakeups(self):
        t0 = 1793000000.0  # 2026-10-26T...
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
        c = self.call
        lines = (c(1, "CronCreate", {"cron": "*/5 * * * *", "prompt": "/check-deploy", "recurring": True}, iso(t0),
                   {"id": "aaaa1111", "humanSchedule": "Every 5 minutes", "recurring": True, "durable": False},
                   "Scheduled recurring job aaaa1111 (Every 5 minutes). Auto-expires after 7 days.")
                 + c(2, "CronCreate", {"cron": "*/9 * * * *", "prompt": "gone\nsecond line", "recurring": True},
                     iso(t0 + 1), {"id": "bbbb2222", "humanSchedule": "Every 9 minutes", "recurring": True})
                 + c(3, "CronDelete", {"id": "bbbb2222"}, iso(t0 + 2), {"id": "bbbb2222"}, "Cancelled job bbbb2222.")
                 + c(4, "CronDelete", {"id": "aaaa1111"}, iso(t0 + 3), None, "error", error=True)
                 + c(5, "CronCreate", {"cron": "30 14 27 10 *", "prompt": "remind me", "recurring": False}, iso(t0 + 4),
                     {"id": "cccc3333", "humanSchedule": "once", "recurring": False, "nextFireAt": (t0 + 3600) * 1000})
                 + c(6, "ScheduleWakeup", {"delaySeconds": 600, "prompt": "/loop watch ci", "reason": "x"}, iso(t0 + 5),
                     {"scheduledFor": (t0 + 605) * 1000, "clampedDelaySeconds": 600, "wasClamped": False}))
        path = self.write_transcript(lines)
        got = transcript.loops("sid", started=t0 - 100, now=t0 + 60)
        self.assertEqual([(l["kind"], l["schedule"], l["prompt"]) for l in got],
                         [("cron", "Every 5 minutes", "/check-deploy"), ("once", got[1]["schedule"], "remind me"),
                          ("wakeup", "/loop", "/loop watch ci")])
        self.assertEqual(got[2]["next"], t0 + 605)
        # Claude restarted after the jobs were made: session-only jobs are gone
        self.assertEqual(transcript.loops("sid", started=t0 + 50, now=t0 + 60), [])
        # the one-shot fired, the wakeup passed while idle, the cron expired after 7 days
        self.assertEqual([l["kind"] for l in transcript.loops("sid", started=t0 - 100, now=t0 + 3700)], ["cron"])
        self.assertEqual([l["kind"] for l in transcript.loops("sid", t0 - 100, busy=True, now=t0 + 700)],
                         ["cron", "once", "wakeup"])  # a /loop turn is running: it will reschedule
        self.assertEqual(transcript.loops("sid", started=t0 - 100, now=t0 + 8 * 86400), [])
        # appended later: CronList is the truth, and stopping the loop ends the wakeup
        with open(path, "a") as f:
            for l in (c(7, "CronList", {}, iso(t0 + 70),
                        {"jobs": [{"id": "dddd4444", "cron": "0 * * * *", "humanSchedule": "Every hour",
                                   "prompt": "hourly", "recurring": True}]})
                      + c(8, "ScheduleWakeup", {"stop": True}, iso(t0 + 71), {"stopped": True, "scheduledFor": 0})):
                f.write(json.dumps(l, separators=(",", ":")) + "\n")
        got = transcript.loops("sid", started=t0 - 100, now=t0 + 80)
        self.assertEqual([(l["schedule"], l["prompt"]) for l in got], [("Every hour", "hourly")])

    def test_loops_survive_a_scan_split_between_call_and_result(self):
        t0 = 1793000000.0
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
        use, res = self.call(1, "CronCreate", {"cron": "*/5 * * * *", "prompt": "p"}, iso(t0),
                             {"id": "aaaa1111", "humanSchedule": "Every 5 minutes", "recurring": True})
        path = self.write_transcript([use])
        self.assertEqual(transcript.loops("sid", now=t0 + 1), [])
        with open(path, "a") as f:
            f.write(json.dumps(res, separators=(",", ":")) + "\n")
        self.assertEqual(len(transcript.loops("sid", now=t0 + 2)), 1)

    def test_once_at(self):
        after = time.mktime((2026, 10, 26, 12, 0, 0, 0, 0, -1))
        self.assertEqual(transcript._once_at("30 14 26 10 *", after), time.mktime((2026, 10, 26, 14, 30, 0, 0, 0, -1)))
        self.assertEqual(transcript._once_at("0 9 1 1 *", after), time.mktime((2027, 1, 1, 9, 0, 0, 0, 0, -1)))
        self.assertEqual(transcript._once_at("*/5 * * * *", after + 61), after + 300)
        self.assertEqual(transcript._once_at("*/5 * * * *", after + 300 + 30), after + 600)  # made mid-minute
        self.assertEqual(transcript._once_at("15 9 * * 1", after), time.mktime((2026, 11, 2, 9, 15, 0, 0, 0, -1)))
        self.assertEqual(transcript._once_at("0 9 1 * *", after), time.mktime((2026, 11, 1, 9, 0, 0, 0, 0, -1)))
        self.assertEqual(transcript._once_at("0 9 1 3 *", after), time.mktime((2027, 3, 1, 9, 0, 0, 0, 0, -1)))
        self.assertIsNone(transcript._once_at("not a cron", after))
        self.assertIsNone(transcript._once_at("0 0 31 2 *", after))  # never: Feb 31

    def test_summary_skips_tool_results_and_meta(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "projects", "-x"))
        lines = [
            {"type": "ai-title", "aiTitle": "Old title"},
            {"type": "user", "message": {"content": "please fix the bug"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it."}]}},
            {"type": "user", "isMeta": True, "message": {"content": "meta stuff"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "user", "message": {"content": "<system-reminder>r</system-reminder>  "}},
            {"type": "custom-title", "customTitle": "New title"},
            "not json",
        ]
        with open(os.path.join(home, "projects", "-x", "sid.jsonl"), "w") as f:
            for l in lines:
                f.write((l if isinstance(l, str) else json.dumps(l)) + "\n")
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = home
        try:
            transcript._paths.clear()
            s = transcript.summary("sid")
            t = transcript.titles("sid")
        finally:
            if old is None:
                del os.environ["CLAUDE_CONFIG_DIR"]
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old
        self.assertEqual(s, {"title": "New title", "user": "please fix the bug", "assistant": "Fixed it."})
        self.assertEqual(t, ("New title", "Old title"))


if __name__ == "__main__":
    unittest.main()


class NotifyTests(unittest.TestCase):
    def snap(self, **statuses):
        return {(1, sid): (st, 1000.0, 0.0) for sid, st in statuses.items()}

    def test_events(self):
        prev = self.snap(a="busy", b="idle", c="busy", d="busy", e="waiting")
        cur = self.snap(a="idle", b="waiting", c="shell", d="busy", e="waiting", f="waiting")
        self.assertEqual(sorted(notify.events(prev, cur)), [((1, "a"), "done"), ((1, "b"), "waiting"), ((1, "c"), "done")])

    def test_quick_turn_after_start_is_not_done(self):
        prev = {(1, "a"): ("busy", 100.0, 95.0)}
        self.assertEqual(notify.events(prev, {(1, "a"): ("idle", 101.0, 95.0)}), [])

    def test_snapshot_skips_non_interactive_and_bad(self):
        snap = notify.snapshot([{"pid": 7, "sessionId": "a", "status": "busy", "statusUpdatedAt": 2000},
                                {"pid": 8, "sessionId": "b", "kind": "bg"}, {"pid": 9, "sessionId": 5},
                                {"sessionId": "nopid"}])
        self.assertEqual(snap, {(7, "a"): ("busy", 2.0, 0.0)})

    def test_stale_file_for_same_session_does_not_mask_live_one(self):
        stale = {"pid": 5, "sessionId": "a", "status": "idle"}
        prev = notify.snapshot([stale, {"pid": 6, "sessionId": "a", "status": "busy"}])
        cur = notify.snapshot([stale, {"pid": 6, "sessionId": "a", "status": "idle"}])
        self.assertEqual(notify.events(prev, cur), [((6, "a"), "done")])

    def test_sound_choice(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": d}):
            self.assertEqual(notify.sound_file("done", {"notify-sound": "off"}), "")
            self.assertEqual(notify.sound_file("done", {"notify-sound": "/x.mp3"}), "/x.mp3")
            self.assertEqual(notify.sound_file("done", {"notify-sound": "on", "notify-sound-done": "/d.oga"}), "/d.oga")
            self.assertIn("/sounds/", notify.sound_file("waiting", {"notify-sound": "system"}).lower())
            # per-event setting works even with the main switch off
            self.assertTrue(notify.sound_file("done", {"notify-sound": "off", "notify-sound-done": "soft"})
                            .startswith(d))
            soft = notify.sound_file("waiting", {"notify-sound": "on"})
            self.assertTrue(soft.startswith(d) and soft.endswith(".wav"))
            # soft sounds have the volume built in; files get it from the player
            self.assertEqual(notify.sound("done", {"notify-sound": "soft", "notify-volume": "40"})[1], 100)
            self.assertEqual(notify.sound("done", {"notify-sound": "/x.mp3", "notify-volume": "40"}), ("/x.mp3", 40))
            self.assertEqual(notify.volume({"notify-volume": "loud"}), 100)
            self.assertEqual(notify.volume({"notify-volume": "250"}), 100)

    def test_soft_sound_is_a_quiet_wav(self):
        import wave
        with tempfile.TemporaryDirectory() as d:
            loud, quiet = notify.soft_sound("done", 100, d), notify.soft_sound("done", 30, d)
            self.assertNotEqual(loud, quiet)
            self.assertEqual(notify.soft_sound("done", 100, d), loud)  # cached
            peaks = []
            for path in (loud, quiet):
                with wave.open(path) as w:
                    self.assertEqual((w.getnchannels(), w.getsampwidth()), (1, 2))
                    self.assertLess(w.getnframes() / w.getframerate(), 1.5)
                    data = w.readframes(w.getnframes())
                peaks.append(max(abs(x) for x in struct.unpack(f"<{len(data) // 2}h", data)))
            self.assertLess(peaks[0], 0.4 * 32767)
            self.assertLess(peaks[1], peaks[0] * 0.4)

    def test_osc_strips_separators(self):
        self.assertEqual(notify.osc_notify("a;b", "c\x1bd"), b"\x1b]777;notify;ab;cd\x1b\\")

    def test_watcher_delay_still_and_visibility(self):
        files = [{"pid": ag("a").pid, "sessionId": "a", "status": "busy"}, {"pid": ag("b").pid, "sessionId": "b", "status": "busy"}]

        class Files:
            def read(self):
                return files

        class Disc:
            tmux = None
            files = Files()

            def agents(self, record_seen=True, strict=False):
                return [ag("a"), ag("b", on_screen=True)]

        sent, now = [], [100.0]
        w = notify.Watcher(Disc(), deliver_fn=lambda t, a, k, o: sent.append((a.session_id, k)),
                           clock=lambda: now[0])
        w._opts_at = 1e18  # keep the defaults (delay 1s, done+waiting, skip visible)
        w.step()
        files[0]["status"] = files[1]["status"] = "idle"
        w.step()
        self.assertEqual(sent, [])            # delay not over yet
        now[0] = 101.5
        w.step()
        self.assertEqual(sent, [("a", "done")])  # b is on screen
        files[0]["status"] = "busy"
        w.step()
        files[0]["status"] = "idle"
        now[0] = 102.0
        w.step()
        files[0]["status"] = "busy"            # back to work before the delay ended
        now[0] = 104.0
        w.step()
        self.assertEqual(sent, [("a", "done")])

    def test_pending_dropped_when_state_does_not_hold(self):
        files = [{"pid": ag("a").pid, "sessionId": "a", "status": "busy"}]

        class Files:
            def read(self):
                return files

        class Disc:
            tmux = None
            files = Files()

            def agents(self, record_seen=True, strict=False):
                return [ag("a")]

        sent, now = [], [100.0]
        w = notify.Watcher(Disc(), deliver_fn=lambda t, a, k, o: sent.append(k), clock=lambda: now[0])
        w._opts_at = 1e18
        w.opts["notify-on"] = "done"
        w.step()
        for st in ("idle", "waiting", "idle"):  # idle never held for the full second
            files[0]["status"] = st
            now[0] += 0.4
            w.step()
        now[0] += 0.3
        w.step()
        self.assertEqual(sent, [])

    def test_stop_existing_ignores_stale_lock(self):
        d = tempfile.mkdtemp()
        with open(notify.lock_path(d, 42), "w") as f:
            f.write("1234567\n")
        killed = []
        real, notify.os.kill = notify.os.kill, lambda pid, sig: killed.append(pid)
        try:
            notify.stop_existing(d, 42)
            self.assertEqual(killed, [])
            lock = notify.hold_lock(d, 42)
            notify.stop_existing(d, 42)  # held, but by us: never signal ourselves
            self.assertEqual(killed, [])
            lock.close()
        finally:
            notify.os.kill = real

    def test_due_event_survives_a_tmux_failure(self):
        files = [{"pid": ag("a").pid, "sessionId": "a", "status": "busy"}]
        fail = [True]

        class Files:
            def read(self):
                return files

        class Disc:
            tmux = None
            files = Files()

            def agents(self, record_seen=True, strict=False):
                if fail[0]:
                    raise RuntimeError("tmux timed out")
                return [ag("a")]

        sent, now = [], [100.0]
        w = notify.Watcher(Disc(), deliver_fn=lambda t, a, k, o: sent.append(k), clock=lambda: now[0])
        w._opts_at = 1e18
        w.step()
        files[0]["status"] = "idle"
        w.step()
        now[0] += 2
        with self.assertRaises(RuntimeError):
            w.step()
        fail[0] = False
        now[0] += 0.5
        w.step()
        self.assertEqual(sent, ["done"])


def write_proc(root, pid, ppid, start, argv):
    """A fake /proc entry with comm and cmdline, like a real process."""
    write_stat(root, pid, ppid, start, comm=os.path.basename(argv[0])[:15])
    with open(os.path.join(root, str(pid), "comm"), "w") as f:
        f.write(os.path.basename(argv[0])[:15] + "\n")
    with open(os.path.join(root, str(pid), "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")


def codex_db(path, rows):
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("create table threads (id text primary key, rollout_path text, cwd text, name text, title text, "
                "created_at integer, updated_at integer, created_at_ms integer, updated_at_ms integer, source text, "
                "thread_source text, originator text, archived integer, first_user_message text)")
    for r in rows:
        r = dict({"rollout_path": "", "cwd": "/w", "name": None, "title": "", "created_at": 0, "updated_at": 0,
                  "created_at_ms": None, "updated_at_ms": None, "source": "cli", "thread_source": "user",
                  "originator": "codex-tui", "archived": 0, "first_user_message": ""}, **r)
        con.execute(f"insert into threads ({', '.join(r)}) values ({', '.join('?' * len(r))})", list(r.values()))
    con.commit()
    con.close()


def rollout(path, events):
    with open(path, "w") as f:
        for ts, typ, payload in events:
            f.write(json.dumps({"timestamp": ts, "type": typ, "payload": payload}) + "\n")


class CodexTests(unittest.TestCase):
    def test_title_status_and_name(self):
        self.assertEqual(codex.parse_title("[ ! ] Action Required | Fix login | api"), ("waiting", "Fix login"))
        self.assertEqual(codex.parse_title("[ . ] Action Required | api"), ("waiting", ""))
        self.assertEqual(codex.parse_title("⠼ Fix login | api"), ("busy", "Fix login"))
        self.assertEqual(codex.parse_title("Fix login | api"), ("idle", "Fix login"))
        self.assertEqual(codex.parse_title(" api"), ("idle", ""))  # older Codex: project only
        self.assertEqual(codex.parse_title(""), ("idle", ""))

    def test_which_processes_are_the_tui(self):
        tui = "/usr/lib/node_modules/@openai/codex/vendor/bin/codex"
        self.assertTrue(codex.is_tui([tui]))
        self.assertTrue(codex.is_tui([tui, "--dangerously-bypass-hook-trust", "resume", "--last"]))
        self.assertTrue(codex.is_tui([tui, "-m", "exec", "fix the tests"]))  # "exec" is the model name here
        self.assertTrue(codex.is_tui([tui, "fix the tests"]))
        self.assertFalse(codex.is_tui([tui, "exec", "--skip-git-repo-check", "review this"]))
        self.assertFalse(codex.is_tui([tui, "-c", "x=1", "review", "--commit", "abc"]))
        self.assertFalse(codex.is_tui([tui, "app-server", "daemon", "pid-update-loop"]))
        self.assertFalse(codex.is_tui(["node", "/usr/bin/codex"]))  # the npm shim; its child is the TUI
        uuid = "01a11994-502f-7042-ac0c-dff6c0dec7c2"
        self.assertEqual(codex.resume_id([tui, "--yolo", "resume", uuid]), uuid)
        self.assertEqual(codex.resume_id([tui, "resume", "--last"]), "")
        self.assertEqual(codex.resume_id([tui, "-m", "gpt-5", "resume", uuid]), uuid)
        self.assertEqual(codex.resume_id([tui, "fork", uuid]), "")
        self.assertEqual(codex.resume_id([tui, "resume", "--", uuid]), uuid)
        self.assertTrue(codex.is_tui([tui, "--", "exec"]))  # a prompt, not the subcommand
        self.assertTrue(codex.is_tui([tui, "--", "--help"]))
        self.assertEqual(codex.resume_id([tui, "--", "resume", uuid]), "")

    def test_process_listing_linux_and_ps(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "self"))
        write_proc(root, 70, 60, 500, ["/x/codex"])
        write_proc(root, 80, 70, 510, ["/x/codex", "app-server", "daemon"])
        write_proc(root, 90, 1, 520, ["/usr/bin/python3", "x.py"])
        got = sorted(proc.processes_named("codex", root))
        self.assertEqual(got, [(70, ["/x/codex"]), (80, ["/x/codex", "app-server", "daemon"])])
        ps = "  70 /opt/homebrew/bin/codex resume\n  71 /bin/zsh -l\n  oops\n"
        self.assertEqual(proc.processes_named("codex", ps=ps), [(70, ["/opt/homebrew/bin/codex", "resume"])])
        # outermost TUI only: one started from inside another TUI's shell is the same pane
        write_proc(root, 95, 70, 530, ["/x/codex"])
        found = codex.tuis(proc.ProcTable(root), proc.processes_named("codex", root))
        self.assertEqual([pid for pid, _ in found], [70])

    def test_start_epoch(self):
        self.assertEqual(proc.start_epoch("Wed Oct 14 10:50:59 2026"), 1791975059.0)
        self.assertEqual(proc.start_epoch("Wed Oct  4 10:50:59 2026"), proc.start_epoch("Wed Oct 4 10:50:59 2026"))
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "stat"), "w") as f:
            f.write("cpu 1 2 3\nbtime 1000\n")
        self.assertEqual(proc.start_epoch(str(5 * os.sysconf("SC_CLK_TCK")), root), 1005.0)
        self.assertEqual(proc.start_epoch("garbage"), 0.0)

    def test_threads_skip_exec_subagents_and_archived(self):
        d = tempfile.mkdtemp()
        codex_db(os.path.join(d, "state_5.sqlite"), [
            {"id": "tui", "updated_at_ms": 5000, "created_at": 1},
            {"id": "exec", "source": "exec", "originator": "codex_exec"},
            {"id": "sub", "source": '{"subagent":"review"}', "thread_source": "subagent"},
            {"id": "old", "archived": 1}])
        codex_db(os.path.join(d, "state_4.sqlite"), [{"id": "stale"}])
        with mock.patch.dict(os.environ, {"CODEX_HOME": d}):
            rows = codex.Threads().rows()
        self.assertEqual([(r["id"], r["updated"], r["created"]) for r in rows], [("tui", 5.0, 1.0)])
        self.assertEqual(codex.Threads(os.path.join(d, "missing.sqlite")).rows(), [])

    def test_matching_conversations_to_tuis(self):
        threads = [{"id": "named", "name": "Fix login", "cwd": "/w", "updated": 50.0},
                   {"id": "resumed", "name": None, "cwd": "/w", "updated": 10.0},
                   {"id": "recent", "name": None, "cwd": "/w", "updated": 300.0},
                   {"id": "other", "name": None, "cwd": "/elsewhere", "updated": 400.0}]
        sessions = [{"pid": 1, "start": 0, "cwd": "/w", "name": "Fix login", "resume": ""},
                    {"pid": 2, "start": 0, "cwd": "/w", "name": "", "resume": "resumed"},
                    {"pid": 3, "start": 200, "cwd": "/w", "name": "", "resume": ""},
                    {"pid": 4, "start": 290, "cwd": "/w", "name": "", "resume": ""}]
        got = {pid: t["id"] for pid, t in codex.match(sessions, threads).items()}
        # pid 4 is newest and picks first; pid 3 then has nothing active since it started
        self.assertEqual(got, {1: "named", 2: "resumed", 4: "recent"})

    def test_rollout_tail(self):
        path = os.path.join(tempfile.mkdtemp(), "r.jsonl")
        msg = lambda role, text: {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}  # noqa: E731
        rollout(path, [
            ("2026-10-14T16:59:54.574Z", "event_msg", {"type": "task_started"}),
            ("2026-10-14T16:59:55.184Z", "response_item", msg("user", "<environment_context>…")),
            ("2026-10-14T16:59:55.551Z", "response_item", msg("user", "Say hello")),
            ("2026-10-14T16:59:57.646Z", "response_item", {"type": "message", "role": "assistant",
                                                           "content": [{"type": "output_text", "text": "Hello"}]}),
            ("2026-10-14T16:59:57.792Z", "event_msg", {"type": "task_complete"})])
        with open(path, "a") as f:
            f.write("{not json\n")
        t = codex.tail(path)
        self.assertEqual((t["user"], t["assistant"]), ("Say hello", "Hello"))
        self.assertAlmostEqual(t["started"], 1791997194.574, places=3)
        self.assertAlmostEqual(t["finished"], 1791997197.792, places=3)
        self.assertEqual(codex.tail(os.path.join(path, "missing")), {})

    def test_discovery_lists_codex_panes(self):
        root, home, sessions = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "self"))
        write_stat(root, 50, 1, 100, comm="bash")                          # pane %1's shell
        write_proc(root, 60, 50, 110, ["node", "/usr/bin/codex"])           # the npm shim
        write_proc(root, 70, 60, 120, ["/x/codex"])                         # the TUI
        write_proc(root, 80, 70, 130, ["/x/codex", "app-server", "daemon"])  # shared app-server
        write_stat(root, 51, 1, 100, comm="bash")                          # pane %2's shell
        write_proc(root, 71, 51, 140, ["/x/codex", "exec", "review this"])  # not interactive
        path = os.path.join(home, "r.jsonl")
        rollout(path, [("2026-10-14T16:59:54.000Z", "event_msg", {"type": "task_started"}),
                       ("2026-10-14T16:59:55.000Z", "response_item",
                        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix it"}]})])
        codex_db(os.path.join(home, "state_5.sqlite"), [
            {"id": "t1", "name": "Fix login", "cwd": "/w/api", "rollout_path": path, "created_at": 50,
             "updated_at": 1791997195}])

        class Tmux(FakeTmux):
            def panes(self, strict=False):
                return [Pane("%1", 50, "$1", "2api", "@1", 1, "node", True, True, "/w/api", "⠼ Fix login | api"),
                        Pane("%2", 51, "$1", "2api", "@2", 2, "claude", True, False, "/w/api", "✳ Something")]

        with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            d = agents.Discovery(Tmux(), sessions, proc=root, state_store=state.State(tempfile.mkdtemp()))
            got = d.agents(record_seen=False)
            self.assertEqual([(a.kind, a.pid, a.session_id, a.status, a.title, a.pane_id, a.tag) for a in got],
                             [("codex", 70, "t1", "busy", "Fix login", "%1", "api")])
            self.assertAlmostEqual(got[0].since, 1791997194.0)
            self.assertEqual(transcript.summary("t1")["user"], "fix it")
            self.assertEqual(d.codex_states(), {70: ("busy", proc.start_epoch("120", root))})

    def test_watcher_notifies_for_codex_title_changes(self):
        status = ["busy"]

        class Files:
            def read(self):
                return []

        class Disc:
            tmux = None
            files = Files()

            def codex_states(self):
                return {70: (status[0], 0.0)}

            def agents(self, record_seen=True, strict=False):
                return [Agent(kind="codex", pid=70, session_id="t1", name="api", cwd="/w", status=status[0],
                              since=None, pane_id="%1")]

        sent, now = [], [100.0]
        w = notify.Watcher(Disc(), deliver_fn=lambda t, a, k, o: sent.append((a.session_id, k)),
                           clock=lambda: now[0])
        w._opts_at = 1e18
        w.step()
        status[0] = "idle"
        now[0] = 101.0
        w.step()
        now[0] = 102.5
        w.step()
        self.assertEqual(sent, [("t1", "done")])
        status[0] = "waiting"
        now[0] = 104.0
        w.step()
        now[0] = 105.5
        w.step()
        self.assertEqual(sent, [("t1", "done"), ("t1", "waiting")])
