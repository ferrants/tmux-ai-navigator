"""tmux-ai-nav: find and jump to AI coding agent sessions running in tmux.

  tmux-ai-nav open --client TTY --pane PANE   open (or close) the picker window; used by the key binding
  tmux-ai-nav ui [--client TTY] [--origin W]  run the picker in the current terminal
  tmux-ai-nav list [--json] [--all]            print the agents found
  tmux-ai-nav seen                             record which panes are on screen (tmux hooks)
  tmux-ai-nav later --pane PANE                toggle the "later" flag on the agent in PANE
  tmux-ai-nav watch [--start|--stop]           notify when agents finish or need you
  tmux-ai-nav watch --test done|waiting        send a sample notification with your settings
  tmux-ai-nav demo                             run the picker on made-up sessions
"""
import argparse
import json
import locale
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict

from .agents import Discovery, fmt_age, sort_agents
from .extras import Extras
from .state import State
from .tmux import SEP, Tmux, TmuxError, from_env

WINDOW_FLAG = "@ainav_window"
ORIGIN_OPT = "@ainav_origin"


def make_tmux(args):
    if args.socket_path or args.socket_name:
        return Tmux(socket_name=args.socket_name, socket_path=args.socket_path)
    return from_env()


def server_flags(args):
    if args.socket_path:
        return ["-S", args.socket_path]
    if args.socket_name:
        return ["-L", args.socket_name]
    return []


def discovery(args, tmux=None, with_extras=False):
    tmux = tmux or make_tmux(args)
    extras = None
    if with_extras:
        command = args.extras if args.extras is not None else tmux_option(tmux, "@ainav-extras")
        try:
            interval = float(tmux_option(tmux, "@ainav-extras-interval") or 5)
        except ValueError:
            interval = 5.0
        extras = Extras(command or None, interval)
    return Discovery(tmux, args.sessions_dir, state_store=State(args.state_dir), extras=extras)


def tmux_option(tmux, name):
    try:
        return tmux.options([name])[name].strip()
    except Exception:  # no server: options simply aren't set
        return ""


def cmd_list(args):
    agents = sort_agents(discovery(args, with_extras=True).agents(include_untracked=args.all, record_seen=False), args.sort)
    if args.json:
        print(json.dumps([dict(asdict(a), location=a.location) for a in agents], indent=2))
        return 0
    for a in agents:
        print("\t".join([a.state_label + (",later" if a.later else ""), fmt_age(a.age()), a.title,
                         a.tag, a.location, a.pane_id or "", a.branch, a.cwd]))
    return 0


def self_command():
    """Command line that re-runs this program from tmux, whether installed or run from a checkout."""
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-c",
            f"import sys; sys.path.insert(0, {src!r}); from tmux_ai_navigator.cli import main; sys.exit(main())"]


def ui_command(args, session_id, pane):
    cmd = self_command() + server_flags(args)
    if args.sessions_dir:
        cmd += ["--sessions-dir", args.sessions_dir]
    if args.state_dir:
        cmd += ["--state-dir", args.state_dir]
    if args.extras is not None:
        cmd += ["--extras", args.extras]
    cmd += ["ui", "--here", pane, "--session", session_id]
    if args.client:
        cmd += ["--client", args.client]
    return cmd


def cmd_open(args):
    """Open the picker for the client that pressed the key: in a popup (tmux 3.2+, --popup),
    or in a temporary window, which pressing the key again inside closes."""
    tmux = make_tmux(args)
    pane = args.pane
    fmt = SEP.join(["#{session_id}", "#{window_id}", f"#{{{WINDOW_FLAG}}}", "#{pane_id}"])
    # Prefer the client's view: a pane in a window linked into several sessions doesn't say
    # which session the user is looking at.
    info = tmux.client_format(args.client, fmt).strip() if args.client else ""
    if not info:
        info = tmux.run("display", "-p", "-t", pane, fmt).strip()
    session_id, window_id, is_picker, pane = (info.split(SEP) + ["", "", "", pane])[:4]
    if args.popup:
        # display-popup doesn't expand formats in its command, hence this detour through
        # run-shell and us; it returns when the popup closes, so don't wait for it
        width, _, height = args.popup.partition("x")
        popup = tmux.base + ["display-popup", "-E", "-w", width or "90%", "-h", height or "85%"]
        if args.client:
            popup += ["-c", args.client]
        subprocess.Popen(popup + [" ".join(shlex.quote(c) for c in ui_command(args, session_id, pane))],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return 0
    if is_picker == "1":
        # pressed inside the picker: close it and go back
        origin = tmux.run("show", "-wqv", "-t", window_id, ORIGIN_OPT, check=False).strip()
        if origin:
            tmux.run("select-window", "-t", origin, check=False)
        tmux.run("kill-window", "-t", window_id, check=False)
        return 0
    # a picker left open in this session (e.g. user switched away): replace it
    for line in tmux.run("list-windows", "-t", session_id, "-F", f"#{{window_id}} #{{{WINDOW_FLAG}}}", check=False).splitlines():
        wid, _, flag = line.partition(" ")
        if flag == "1":
            tmux.run("kill-window", "-t", wid, check=False)
    cmd = ui_command(args, session_id, pane) + ["--origin", window_id]
    new = tmux.run("new-window", "-t", f"{session_id}:", "-n", "agents", "-P", "-F", "#{window_id}",
                   " ".join(shlex.quote(c) for c in cmd)).strip()
    tmux.run("set", "-w", "-t", new, WINDOW_FLAG, "1", check=False)
    tmux.run("set", "-w", "-t", new, ORIGIN_OPT, window_id, check=False)
    tmux.run("set", "-w", "-t", new, "remain-on-exit", "off", check=False)
    return 0


def cmd_ui(args):
    from .ui import pick

    locale.setlocale(locale.LC_ALL, "")
    tmux = make_tmux(args)
    disc = discovery(args, tmux, with_extras=True)
    current = args.session
    if not current and args.here:
        current = tmux.run("display", "-p", "-t", args.here, "#{session_id}", check=False).strip() or None
    choice = pick(lambda: disc.agents(current_session=current), here_pane=args.here,
                  toggle_later=disc.toggle_later, warning=lambda: disc.extras.error)
    if choice is None:
        if args.origin:
            tmux.run("select-window", "-t", args.origin, check=False)
        return 0
    client = args.client
    if client and client not in tmux.clients():
        client = None  # the client that opened us is gone; fall back to tmux's notion of current
    try:
        tmux.jump(client, choice.tmux_session_id, choice.window_id, choice.pane_id)
    except TmuxError as e:
        tmux.run("display-message", f"tmux-ai-nav: can't jump to {choice.title}: {e}", check=False)
        if args.origin:
            tmux.run("select-window", "-t", args.origin, check=False)
        return 1
    seen_now(disc, extra=[choice.pane_id])
    return 0


def seen_now(disc, extra=(), at=None):
    visible = {p.pane_id for p in disc.tmux.panes() if p.on_screen}
    disc.update_seen(visible, extra=extra, at=at)


def cmd_seen(args):
    """Hook target: must stay quiet and never fail loudly (run-shell output lands in panes).

    --delay waits before looking (a detaching client is still listed while its hook runs)
    but records the time the hook fired, so a session finishing during the wait still
    counts as finished after it left the screen.
    """
    try:
        at = time.time()
        if args.delay:
            time.sleep(args.delay)
        seen_now(discovery(args), at=at)
    except Exception:
        pass
    return 0


def cmd_later(args):
    tmux = make_tmux(args)
    disc = discovery(args, tmux)
    agent = next((a for a in disc.agents(record_seen=False) if a.pane_id == args.pane), None)
    if not agent:
        tmux.run("display-message", "tmux-ai-nav: no Claude session in this pane", check=False)
        return 1
    on = disc.toggle_later(agent)
    tmux.run("display-message", f"{'flagged' if on else 'unflagged'} for later: {agent.title}", check=False)
    return 0


def cmd_watch(args):
    """--start (from the plugin): replace this server's watcher with a fresh detached one,
    or just stop it when notifications are off. --stop: stop it."""
    from . import notify

    disc = discovery(args)
    if args.test:
        from .agents import Agent
        sample = Agent(kind="claude", pid=0, session_id="test", name="example", cwd=os.getcwd(), status="idle",
                       since=time.time(), tmux_session="example", window_name="test", title="Test notification")
        notify.deliver(disc.tmux, sample, args.test, notify.options(disc.tmux))
        return 0
    if not (args.start or args.stop):
        return notify.watch(disc)
    server = disc.server()
    notify.stop_existing(disc.state.dir, server)
    if args.stop or (tmux_option(disc.tmux, "@ainav-notify") or "tmux").strip() == "off":
        return 0
    cmd = self_command() + server_flags(args)
    if args.sessions_dir:
        cmd += ["--sessions-dir", args.sessions_dir]
    if args.state_dir:
        cmd += ["--state-dir", args.state_dir]
    subprocess.Popen(cmd + ["watch"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return 0


def cmd_demo(args):
    from . import demo
    from .ui import pick

    locale.setlocale(locale.LC_ALL, "")
    data = demo.Demo()
    choice = pick(data.agents, here_pane=data.here, toggle_later=data.toggle_later, summarize=data.summary)
    if choice is not None:
        print(f"would jump to {choice.location} ({choice.title})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tmux-ai-nav", description=__doc__.split("\n")[0])
    ap.add_argument("-L", dest="socket_name", help="tmux socket name")
    ap.add_argument("-S", dest="socket_path", help="tmux socket path")
    ap.add_argument("--sessions-dir", help="Claude sessions dir (default ~/.claude/sessions)")
    ap.add_argument("--state-dir", help="seen/later state dir (default ~/.local/state/tmux-ai-navigator)")
    ap.add_argument("--extras", help="extras command (default: the @ainav-extras tmux option)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="print agents")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include sessions not running in tmux")
    p.add_argument("--sort", choices=["priority", "session"], default="priority")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("seen", help="record panes on screen as seen (for tmux hooks)")
    p.add_argument("--delay", type=float, default=0, help="seconds to wait before looking")
    p.set_defaults(func=cmd_seen)

    p = sub.add_parser("later", help="toggle the later flag on the agent in a pane")
    p.add_argument("--pane", required=True)
    p.set_defaults(func=cmd_later)

    p = sub.add_parser("open", help="toggle the picker window (for key bindings)")
    p.add_argument("--client", help="#{client_tty} of the client to switch")
    p.add_argument("--pane", required=True, help="#{pane_id} the key was pressed in")
    p.add_argument("--popup", metavar="WxH", help="open in a popup of this size (tmux 3.2+), e.g. 90%%x85%%")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("ui", help="run the picker in this terminal")
    p.add_argument("--client")
    p.add_argument("--origin", help="window to return to on quit")
    p.add_argument("--here", help="pane the picker was opened from")
    p.add_argument("--session", help="tmux session the picker was opened from")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("watch", help="notify when agents finish or need you (runs until tmux exits)")
    p.add_argument("--start", action="store_true", help="start a detached watcher, replacing a running one")
    p.add_argument("--stop", action="store_true", help="stop this tmux server's watcher")
    p.add_argument("--test", choices=["done", "waiting"], help="send a sample notification with your settings")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("demo", help="run the picker on made-up sessions (no tmux needed)")
    p.set_defaults(func=cmd_demo)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        args.cmd = "ui"
        args.client = args.origin = args.here = args.session = None
        args.func = cmd_ui
    try:
        return args.func(args)
    except TmuxError as e:
        print(f"tmux-ai-nav: {e}", file=sys.stderr)
        return 1
