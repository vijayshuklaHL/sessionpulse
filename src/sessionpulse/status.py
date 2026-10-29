"""`status.json`: a small, token-free summary for panel widgets such as the GNOME extension.

`sessionpulse daemon` rewrites it every few seconds (atomically, mode 0600) and
removes it on a clean exit. A reader should treat it as stale when `updated` is
older than about three intervals: the daemon was killed or the machine slept.
"""
import json
import os
import time
from pathlib import Path

from . import __version__, config, reader, todos, tray

SCHEMA = 1
INTERVAL = 5  # seconds between writes


def path(cfg: config.Config) -> Path:
    return cfg.data / "status.json"


def build(cfg: config.Config, snap, today=None):
    v = tray.summary(snap, today=today)
    now = int(time.time() * 1000)
    return {
        "schema": SCHEMA,
        "version": __version__,
        "updated": now,
        "interval": INTERVAL,
        "pid": os.getpid(),
        "port": cfg.port,
        "state": v["icon"],  # busy | idle | none
        "open": len(v["live"]),
        "working": v["busy"],
        "sessions": [{
            "project": s.get("project") or "?",
            "state": "busy" if s.get("state") == "busy" else "idle",
            "started": (s.get("live") or {}).get("startedAt") or now,
        } for s in v["live"]],
        "today": {
            "sessions": v["today_sessions"],
            "active_hours": round(v["today_hours"], 2),
            "output_tokens": int(v["today_output"]),
        },
        "todos_open": sum(1 for t in snap.get("todos") or [] if todos.is_open(t)),
        "warnings": len(v["warnings"]),
    }


def write(cfg: config.Config, data):
    """Atomic replace, readable only by you (project names are private enough)."""
    target = path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".status.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, target)


def write_now(cfg: config.Config, today=None):
    with reader.lock:
        snap = reader.snapshot(cfg)
    data = build(cfg, snap, today=today)
    write(cfg, data)
    return data


def run(cfg: config.Config, stop, on_error=None):
    """Write status.json every INTERVAL seconds until `stop` (a threading.Event) is set, then remove it."""
    try:
        while not stop.is_set():
            try:
                write_now(cfg)
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                if on_error:
                    on_error(exc)
            stop.wait(INTERVAL)
    finally:
        remove(cfg)


def remove(cfg: config.Config):
    try:
        path(cfg).unlink()
    except FileNotFoundError:
        pass


def age(cfg: config.Config):
    """Seconds since the daemon last wrote status.json, or None if there is none."""
    try:
        data = json.loads(path(cfg).read_text())
        return max(0.0, time.time() - data["updated"] / 1000)
    except (OSError, ValueError, KeyError, TypeError):
        return None
