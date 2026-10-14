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

from tmux_ai_navigator import agents, claude, extras, notify, proc, state, transcript  # noqa: E402
from tmux_ai_navigator.agents import Agent, arrange, clean_title, fmt_age, map_to_panes, pick_title, sort_agents  # noqa: E402
from tmux_ai_navigator.tmux import Pane  # noqa: E402
from tmux_ai_navigator.ui import build_items, fit, matches, width  # noqa: E402


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
    return Agent(kind="claude", pid=hash(name) & 0xffff, session_id=name, name=name, cwd="/x",
                 status=status, since=since, tmux_session=session, window_index=index,
                 pane_id="%" + name, **kw)


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
        items, _ = build_items(a, "", 0, "priority", False, now)
        self.assertEqual([i[0] for i in items], ["header", "agent", "header", "more"])
        items, _ = build_items(a, "", 0, "priority", True, now)
        self.assertEqual([i[0] for i in items], ["header", "agent", "header", "agent"])
        items, _ = build_items(a, "old", 0, "priority", False, now)
        self.assertEqual([i[0] for i in items], ["header", "agent"])

    def test_filters(self):
        now = self.NOW
        a = [ag("w", "waiting", since=now), ag("b", "busy", since=now), ag("i", "idle", since=now)]
        names = lambda f: [i[1].name for i in build_items(a, "", f, "priority", True, now)[0] if i[0] == "agent"]
        self.assertEqual(names(1), ["w"])
        self.assertEqual(names(2), ["b"])
        self.assertEqual(names(3), ["i"])

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

    def test_store_roundtrip(self):
        st = state.State(tempfile.mkdtemp())
        with st.update() as d:
            state.toggle_later(d, "s1", now=5.0)
        self.assertEqual(st.read()["later"], {"s1": 5.0})
        self.assertEqual(st.read()["servers"], {})


class TranscriptTests(unittest.TestCase):
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
