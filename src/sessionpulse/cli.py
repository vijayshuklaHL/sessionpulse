"""Command line: sessionpulse <command>."""
import argparse
import json
import socket
import sys
import webbrowser

from . import __version__, config, followups, hook, install, reader, server, todos, tray


def cmd_serve(cfg, args):
    srv = server.make_server(cfg)
    followups.start_worker(cfg)
    print(f"Session Pulse on {cfg.url}\nOpen it with: sessionpulse open")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_tray(cfg, args):
    tray.run(cfg)


def cmd_open(cfg, args):
    webbrowser.open(server.open_url(cfg))


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
    missing = tray.available()
    print(f"  {'ok ' if not missing else '-- '}  {'panel icon available' if not missing else missing}")
    with socket.socket() as s:
        busy = s.connect_ex(("127.0.0.1", cfg.port)) == 0
    print(f"  {'ok ' if busy else '-- '}  dashboard {'running' if busy else 'not running'} on {cfg.url}")
    for w in h["warnings"]:
        line(False, w)
    return 0 if ok else 1


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
    show = [i for i in items if bool(i["done_at"]) == (args.action == "list-done")]
    for i in show:
        tag = f"  [{i['project']}]" if i.get("project") else ""
        when = f"  done {i['done_at'][:10]}" if i["done_at"] else ""
        print(f"{i['id']}  {i['text']}{tag}{when}")
    if not show:
        print("Nothing here." if args.action == "list-done" else "No open to-dos.")


def cmd_followups(cfg, args):
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


def main(argv=None):
    p = argparse.ArgumentParser(prog="sessionpulse", description=__doc__)
    p.add_argument("--version", action="version", version=f"sessionpulse {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install", help="add Claude Code hooks and login autostart") \
       .add_argument("--no-autostart", action="store_true", help="only add the hooks")
    sub.add_parser("uninstall", help="remove the hooks and autostart (keeps your data)")
    sub.add_parser("tray", help="panel icon + dashboard server")
    sub.add_parser("serve", help="dashboard server only")
    sub.add_parser("open", help="open the dashboard in your browser")
    sub.add_parser("doctor", help="check the setup and data formats")
    sub.add_parser("snapshot", help="print the dashboard data as JSON")
    t = sub.add_parser("todo", help="your to-do list: add, done, undo, delete, list, list-done")
    t.add_argument("action", nargs="?", default="list",
                   choices=["list", "list-done", "add", "done", "undo", "delete"])
    t.add_argument("text", nargs="*", help="the to-do text (add) or item ids (done/undo/delete)")
    t.add_argument("-p", "--project", help="tag the new to-do with a project")
    f = sub.add_parser("followups", help="pick open follow-ups out of recent sessions with Claude")
    f.add_argument("--days", type=int, help="look back this many days (default: followups_days)")
    f.add_argument("--session", help="only this session id (or its first characters)")
    f.add_argument("--now", action="store_true", help="include sessions that are still active")
    f.add_argument("--again", action="store_true", help="re-read sessions already read")
    sub.add_parser("hook", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    cfg = config.load()
    return globals()[f"cmd_{args.cmd}"](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())
