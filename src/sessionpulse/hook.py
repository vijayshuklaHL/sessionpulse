"""`sessionpulse hook`: run by Claude Code on SessionStart and SessionEnd.

Appends one line to <data_dir>/events.jsonl. It must never fail loudly or
slow the session down, so every error is swallowed into errors.log.
"""
import json
import os
import socket
import sys
from datetime import datetime, timezone

from . import config


def run(stdin=sys.stdin):
    cfg = config.load()
    try:
        hook = json.load(stdin)
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": hook.get("hook_event_name"),
            "session": hook.get("session_id"),
            "cwd": hook.get("cwd"),
            "host": socket.gethostname(),
            # SessionEnd sends "reason" (clear, logout, prompt_input_exit, other);
            # SessionStart sends "source" (startup, resume, clear, compact).
            "reason": hook.get("reason") or hook.get("source"),
        }
        cfg.data.mkdir(parents=True, exist_ok=True)
        with open(cfg.events_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as exc:  # noqa: BLE001 - never break the user's session
        try:
            cfg.data.mkdir(parents=True, exist_ok=True)
            with open(cfg.data / "errors.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} hook {exc!r}\n")
        except OSError:
            pass
    return 0
