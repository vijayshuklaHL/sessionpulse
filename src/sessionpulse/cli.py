"""Command line: sessionpulse <command>."""
import argparse
import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from collections import Counter

from . import __version__, config, followups, hook, install, reader, server, status, todos, tray


def headless():
    """True on a Linux/BSD box with no desktop session, e.g. a server you reach over SSH."""
    return (sys.platform not in ("darwin", "win32")
            and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))


def remote_hint(cfg):
    """How to reach the dashboard from another computer through an SSH tunnel."""
    host = f"{getpass.getuser()}@{socket.gethostname()}"
    return (f"No desktop here, so open it from your own computer through an SSH tunnel:\n"
            f"  ssh -N -L {cfg.port}:127.0.0.1:{cfg.port} {host}\n"
            f"then browse to:\n  {server.open_url(cfg)}\n"
            f"(Use the address you normally ssh to. If port {cfg.port} is busy on your computer, "
            f"use another local port, e.g. -L 18765:127.0.0.1:{cfg.port}, and change it in the link.)")


def cmd_serve(cfg, args):
    try:
        srv = server.make_server(cfg)
    except OSError as exc:
        raise SystemExit(f"Can't serve on port {cfg.port}: {exc.strerror or exc}. Something else is using it "
                         f"(another `sessionpulse serve`/`tray`?); stop that, or set `port` in {config.CONFIG_FILE}.")
    followups.start_worker(cfg)
    print(f"Session Pulse on {cfg.url}\n" + (remote_hint(cfg) if headless() else "Open it with: sessionpulse open"),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_daemon(cfg, args):
    """Dashboard server + follow-ups + status.json, no icon: for the GNOME extension."""
    try:
        srv = server.make_server(cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        followups.start_worker(cfg)
        print(f"Session Pulse on {cfg.url}", flush=True)
    except OSError:
        print(f"Port {cfg.port} is already in use (another `sessionpulse serve`/`tray`?); "
              "writing status.json only.", flush=True)
    print(f"Writing {status.path(cfg)} every {status.INTERVAL}s", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    status.run(cfg, stop, on_error=lambda exc: print(f"status.json not written: {exc}",
                                                    file=sys.stderr, flush=True))


def cmd_tray(cfg, args):
    tray.run(cfg)


def cmd_open(cfg, args):
    if headless() or not webbrowser.open(server.open_url(cfg)):
        print(remote_hint(cfg) if headless() else f"Couldn't start a browser. Open:\n  {server.open_url(cfg)}")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", cfg.port)) != 0:
            print("Note: the dashboard isn't running. Start it with `sessionpulse serve` (or `sessionpulse tray`).")


def cmd_hook(cfg, args):
    return hook.run()


def cmd_install(cfg, args):
    try:
        changed = install.install_hooks(cfg)
    except install.SettingsError as exc:
        sys.exit(f"error: {exc}")
    print(("Added" if changed else "Already present:") +
          f" SessionStart/SessionEnd hooks in {cfg.claude / 'settings.json'}")
    if args.no_autostart:
        return
    if args.daemon:
        print(f"Background daemon will start at login ({install.enable_autostart('daemon')}).")
        print("Start it now with: sessionpulse daemon &")
        return
    missing = tray.available()
    if missing:
        print(f"Skipped login autostart: {missing}\nYou can still run: sessionpulse serve")
        return
    print(f"Panel icon will start at login ({install.enable_autostart()}).")
    print("Start it now with: sessionpulse tray &")


def cmd_uninstall(cfg, args):
    try:
        changed = install.uninstall_hooks(cfg)
    except install.SettingsError as exc:
        sys.exit(f"error: {exc}")
    print("Removed Session Pulse hooks." if changed else "No Session Pulse hooks were installed.")
    if install.disable_autostart():
        print("Removed login autostart.")
    print(f"Your data is kept in {cfg.data}; delete that folder to remove it.")


def cmd_doctor(cfg, args):
    ok = True

    def line(good, text):
        nonlocal ok
        ok &= bool(good)
        print(f"  {'ok ' if good else 'FIX'}  {text}")

    print(f"Session Pulse {__version__}")
    line(cfg.claude.is_dir(), f"Claude Code folder: {cfg.claude}")
    line((cfg.claude / "sessions").is_dir(), "live-session files (sessions/)")
    snap = reader.snapshot(cfg)
    h = snap["health"]
    line(h["files"] > 0, f"{h['files']} transcripts, {len(snap['sessions'])} sessions, "
                         f"newest Claude Code {h['newest_cli'] or '?'}")
    try:
        inst = install.hooks_installed(cfg)
        line(inst == set(install.EVENTS), "start/exit hooks installed" if inst == set(install.EVENTS)
             else "start/exit hooks missing: run `sessionpulse install`")
    except install.SettingsError as exc:
        line(False, str(exc))
    if headless():
        print("  --   no desktop session: the panel icon and login autostart don't apply here. "
              "Run `sessionpulse serve` (see \"On a server\" in the README) and use `sessionpulse open` "
              "for the SSH tunnel link.")
    else:
        missing = tray.available()
        print(f"  {'ok ' if not missing else '-- '}  {'panel icon available' if not missing else missing}")
    with socket.socket() as s:
        busy = s.connect_ex(("127.0.0.1", cfg.port)) == 0
    print(f"  {'ok ' if busy else '-- '}  dashboard {'running' if busy else 'not running'} on {cfg.url}"
          + ("" if busy else ": start it with `sessionpulse serve`"))
    a = status.age(cfg)
    if a is not None:
        fresh = a < status.INTERVAL * 3
        print(f"  {'ok ' if fresh else '-- '}  status.json {'updated' if fresh else 'stale, last updated'} "
              f"{int(a)}s ago{'' if fresh else ': is `sessionpulse daemon` running?'}")
    bad = followups.failing(cfg)
    if bad:
        sid, e = bad[0]
        print(f"  --   {len(bad)} session(s) failed when read for follow-ups; the latest error: "
              f"{e.get('last_error')}. See `sessionpulse followups status`.")
    week = followups.usage_totals(cfg, 7)
    if week["calls"]:
        print(f"  ok   follow-ups used {week['calls']} model calls, {tokens(week['tokens'])} tokens in the last 7 days")
    for w in h["warnings"]:
        line(False, w)
    return 0 if ok else 1


def tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def cmd_snapshot(cfg, args):
    json.dump(reader.snapshot(cfg), sys.stdout, indent=1)


def cmd_todo(cfg, args):
    try:
        if args.action == "add":
            todos.apply(cfg, {"op": "add", "text": " ".join(args.text), "project": args.project})
        elif args.action in ("done", "undo", "delete"):
            for i in args.text:
                todos.apply(cfg, {"op": args.action, "id": i})
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    items = todos.load(cfg)
    want = (lambda i: i["done_at"]) if args.action == "list-done" else todos.is_open
    show = [i for i in items if want(i)]
    for i in show:
        tag = f"  [{i['project']}]" if i.get("project") else ""
        when = f"  done {i['done_at'][:10]}" if i["done_at"] else ""
        print(f"{i['id']}  {i['text']}{tag}{when}")
    parked = [i for i in items if not i["done_at"] and i.get("status") in todos.STATUSES]
    if parked and args.action != "list-done":
        held = sum(i["status"] == "hold" for i in parked)
        print(f"({held} on hold, {len(parked) - held} archived; see the dashboard board)")
    if not show:
        print("Nothing here." if args.action == "list-done" else "No open to-dos.")


def followups_status(cfg):
    print(f"Follow-ups are {'on' if cfg.followups else 'off (add `followups = true` to config.toml)'}; "
          f"model {cfg.followups_model}.")
    st = followups.load_state(cfg)
    read = [e for e in st.values() if isinstance(e, dict) and e.get("at")]
    print(f"Sessions read: {len(read)}, adding {sum(e.get('added') or 0 for e in read)} "
          f"and closing {sum(e.get('closed') or 0 for e in read)} to-dos.")
    print("Model use (the cost is what the API would charge; on a subscription it counts toward your limits):")
    for label, days in (("today", 1), ("7 days", 7), ("30 days", 30)):
        u = followups.usage_totals(cfg, days)
        print(f"  {label:8} {u['calls']:4} calls  {tokens(u['tokens']):>6} tokens "
              f"({tokens(u['output_tokens'])} written)  ${u['cost_usd']:.2f}")
    bad = followups.failing(cfg)
    if not bad:
        print("No session is failing.")
        return
    print(f"{len(bad)} session(s) failed when last read:")
    for sid, e in bad:
        when = f"next try after {e['next_try'][:16].replace('T', ' ')}" if e.get("next_try") else \
            "waiting until the session grows"
        print(f"  {sid[:8]}  {e.get('project') or '?'}: {e['fails']}x, {when}\n            {e.get('last_error')}")
    print("Retry one now with `sessionpulse followups --session <id>`.")


def cmd_followups(cfg, args):
    if args.action == "status":
        return followups_status(cfg)
    if not followups.claude_bin(cfg):
        sys.exit("error: the `claude` command was not found; set claude_command in config.toml")
    try:
        n, added, closed = followups.scan(cfg, days=args.days, idle_minutes=0 if args.now else None,
                                          session=args.session, force=args.again,
                                          log=lambda m: print(m, flush=True))
    except (RuntimeError, OSError, ValueError) as exc:
        sys.exit(f"error: {exc}")
    print(f"Read {n} session(s): {added} follow-up(s) added, {closed} closed.")
    if not cfg.followups:
        print("Tip: add `followups = true` to ~/.config/sessionpulse/config.toml to do this automatically.")


def cmd_dedupe(cfg, args):
    if not followups.claude_bin(cfg):
        sys.exit("error: the `claude` command was not found; set claude_command in config.toml")
    items = todos.load(cfg)
    counts = Counter(i.get("project") or "" for i in items if todos.is_open(i))
    projects = [args.project] if args.project is not None else [p for p, n in counts.items() if n > 1]
    text = {i["id"]: i["text"] for i in items}
    total = 0
    for project in projects:
        try:
            groups = followups.find_duplicates(cfg, project)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"{project or '(no project)'}: skipped, {exc}", flush=True)
            continue
        if not groups:
            continue
        print(f"\n{project or '(no project)'}", flush=True)
        for g in groups:
            print("  same:")
            for x in g:
                print(f"    {x}  {text.get(x, '')}")
        if args.apply:
            n = todos.merge_duplicates(cfg, groups)
        else:
            n = sum(len(g) - 1 for g in groups)
        total += n
    if args.apply:
        print(f"\nArchived {total} duplicate(s); each keeps `duplicate_of` pointing at the one kept.")
    else:
        print(f"\n{total} duplicate(s) found. Run again with --apply to archive them, keeping the newest.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="sessionpulse", description=__doc__)
    p.add_argument("--version", action="version", version=f"sessionpulse {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="add Claude Code hooks and login autostart")
    i.add_argument("--no-autostart", action="store_true", help="only add the hooks")
    i.add_argument("--daemon", action="store_true",
                   help="autostart the background daemon (for the GNOME extension) instead of the panel icon")
    sub.add_parser("uninstall", help="remove the hooks and autostart (keeps your data)")
    sub.add_parser("tray", help="panel icon + dashboard server")
    sub.add_parser("serve", help="dashboard server only")
    sub.add_parser("daemon", help="dashboard server + status.json for the GNOME extension, no icon")
    sub.add_parser("open", help="open the dashboard in your browser")
    sub.add_parser("doctor", help="check the setup and data formats")
    sub.add_parser("snapshot", help="print the dashboard data as JSON")
    t = sub.add_parser("todo", help="your to-do list: add, done, undo, delete, list, list-done")
    t.add_argument("action", nargs="?", default="list",
                   choices=["list", "list-done", "add", "done", "undo", "delete"])
    t.add_argument("text", nargs="*", help="the to-do text (add) or item ids (done/undo/delete)")
    t.add_argument("-p", "--project", help="tag the new to-do with a project")
    f = sub.add_parser("followups", help="pick open follow-ups out of recent sessions with Claude")
    f.add_argument("action", nargs="?", default="run", choices=["run", "status"],
                   help="run (default), or status: model use and sessions that failed")
    f.add_argument("--days", type=int, help="look back this many days (default: followups_days)")
    f.add_argument("--session", help="only this session id (or its first characters)")
    f.add_argument("--now", action="store_true", help="include sessions that are still active")
    f.add_argument("--again", action="store_true", help="re-read sessions already read")
    d = sub.add_parser("dedupe", help="find open to-dos that are the same work reworded, with Claude")
    d.add_argument("-p", "--project", help="only this project (default: every project)")
    d.add_argument("--apply", action="store_true", help="archive the copies (default: only list them)")
    sub.add_parser("hook", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    cfg = config.load()
    return globals()[f"cmd_{args.cmd}"](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())
