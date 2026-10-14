"""`sessionpulse tray`: panel icon, plus the dashboard server in the same process.

Next to the icon is the number of open sessions. The dot is orange while any
session is working, green when all are idle, and grey when none are running.

Needs PyGObject and the Ayatana AppIndicator typelib, e.g. on Ubuntu:
    sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
GNOME also needs the AppIndicator extension (on by default on Ubuntu).
"""
import threading
import time
import webbrowser
from datetime import datetime
from importlib import resources

from . import config, followups, reader, server

SLOTS = 8  # fixed rows for open sessions: replacing the whole menu would close it while open
LIVE = ("busy", "idle", "running")


def available():
    """None if the panel icon can run here, else a sentence saying what's missing."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, Gtk  # noqa: F401
    except (ImportError, ValueError) as exc:
        return (f"panel icon unavailable ({exc}). On Ubuntu/Debian: "
                "sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1, and install "
                "Session Pulse with `pipx install --system-site-packages`.")
    return None


def short(s, n=46):
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_n(n):
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.1f}k" if n >= 1e3 else str(int(n))


def fmt_h(h):
    return f"{h:.1f}h" if h >= 1 else f"{round(h*60)}m"


def fmt_dur(ms):
    m = int(ms / 60000)
    return f"{m}m" if m < 60 else f"{m//60}h {m%60:02d}m"


def summary(snap, today=None):
    """Plain-data view of a snapshot for the menu (kept separate so it is testable)."""
    sessions = snap["sessions"]
    live = sorted((s for s in sessions if s.get("state") in LIVE),
                  key=lambda s: -((s.get("live") or {}).get("updatedAt") or 0))
    busy = sum(s["state"] == "busy" for s in live)
    day = today or datetime.now().date().isoformat()
    return {
        "live": live, "busy": busy,
        "icon": "busy" if busy else "idle" if live else "none",
        "today_sessions": sum(1 for s in sessions if day in s.get("hours_by_day", {})),
        "today_hours": sum(s.get("hours_by_day", {}).get(day, 0) for s in sessions),
        "today_output": sum(v for s in sessions for k, v in s.get("days", {}).get(day, {}).items()
                            if k.endswith("|output")),
        "warnings": snap.get("health", {}).get("warnings", []),
    }


def run(cfg: config.Config):
    missing = available()
    if missing:
        raise SystemExit(missing)
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AI, GLib, Gtk

    try:
        srv = server.make_server(cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        followups.start_worker(cfg)
    except OSError:
        pass  # something already serves this port (e.g. `sessionpulse serve`); use it
    url = server.open_url(cfg)
    icons = str(resources.files("sessionpulse").joinpath("icons"))

    ind = AI.Indicator.new("sessionpulse", "sessionpulse-none", AI.IndicatorCategory.APPLICATION_STATUS)
    ind.set_icon_theme_path(icons)
    ind.set_status(AI.IndicatorStatus.ACTIVE)
    ind.set_title("Session Pulse")

    def item(menu, label, cb=None, sensitive=True):
        it = Gtk.MenuItem(label=label)
        it.set_sensitive(sensitive)
        if cb:
            it.connect("activate", cb)
        menu.append(it)
        return it

    open_dash = lambda *_: webbrowser.open(url)  # noqa: E731
    menu = Gtk.Menu()
    header = item(menu, "Loading…", sensitive=False)
    menu.append(Gtk.SeparatorMenuItem())
    rows = [item(menu, "", open_dash) for _ in range(SLOTS)]
    more = item(menu, "", open_dash)
    menu.append(Gtk.SeparatorMenuItem())
    today = item(menu, "", sensitive=False)
    warn = item(menu, "", open_dash)
    menu.append(Gtk.SeparatorMenuItem())
    item(menu, "Open dashboard", open_dash)
    board = item(menu, "Open to-do board", lambda *_: webbrowser.open(url + "#board"))
    item(menu, "Quit", lambda *_: Gtk.main_quit())
    menu.show_all()
    ind.set_menu(menu)

    def apply(snap):
        v = summary(snap)
        live = v["live"]
        ind.set_icon_full(f"sessionpulse-{v['icon']}", f"{len(live)} Claude Code sessions")
        ind.set_label(str(len(live)) if live else "", "00")
        header.set_label(f"{len(live)} open · {v['busy']} working · {len(live) - v['busy']} idle"
                         if live else "No Claude Code sessions running")
        now = time.time() * 1000
        for i, row in enumerate(rows):
            if i < len(live):
                s = live[i]
                working = s["state"] == "busy"
                started = (s.get("live") or {}).get("startedAt") or now
                row.set_label(f"{'●' if working else '○'}  {short(s['project'])}  —  "
                              f"{'working' if working else 'idle'} · {fmt_dur(now - started)}")
                row.show()
            else:
                row.hide()
        extra = len(live) - SLOTS
        more.set_label(f"+{extra} more…")
        more.set_visible(extra > 0)
        today.set_label(f"Today: {v['today_sessions']} sessions · {fmt_h(v['today_hours'])} active · "
                        f"{fmt_n(v['today_output'])} output tokens")
        warn.set_label(f"⚠ {len(v['warnings'])} data warning(s): see dashboard")
        warn.set_visible(bool(v["warnings"]))
        n = sum(1 for t in snap.get("todos") or [] if not t.get("done_at"))
        board.set_label(f"Open to-do board ({n} open)" if n else "Open to-do board")
        return False

    def collect():
        try:
            with reader.lock:
                snap = reader.snapshot(cfg)
        except Exception as exc:  # noqa: BLE001 - keep the icon alive
            GLib.idle_add(header.set_label, f"Error reading sessions: {exc}")
            return
        GLib.idle_add(apply, snap)

    def tick():
        threading.Thread(target=collect, daemon=True).start()
        return True

    tick()
    GLib.timeout_add_seconds(max(1, int(cfg.refresh_seconds)), tick)
    Gtk.main()
