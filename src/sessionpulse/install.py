"""Adds and removes Session Pulse's hooks in Claude Code's settings.json,
and its login autostart entry.

Our hook entries are recognised by HOOK_MARK in the command, so install is
idempotent (re-running updates the interpreter path) and uninstall removes
only what we added. settings.json is backed up before every change and
written atomically; if it isn't valid JSON we refuse to touch it.
"""
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .config import Config

HOOK_MARK = "-m sessionpulse hook"
EVENTS = ("SessionStart", "SessionEnd")
AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "sessionpulse.desktop"


class SettingsError(Exception):
    pass


def hook_command():
    return f'"{sys.executable}" {HOOK_MARK}'


def _settings_path(cfg: Config) -> Path:
    return cfg.claude / "settings.json"


def _load(path: Path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc}); not changing it.") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} is not a JSON object; not changing it.")
    return data


def _save(path: Path, data):
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.sessionpulse-backup-{stamp}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".sessionpulse-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _ours(hook):
    return isinstance(hook, dict) and HOOK_MARK in str(hook.get("command", ""))


def hooks_installed(cfg: Config):
    data = _load(_settings_path(cfg))
    found = set()
    for ev in EVENTS:
        for group in (data.get("hooks") or {}).get(ev) or []:
            if any(_ours(h) for h in (group or {}).get("hooks") or []):
                found.add(ev)
    return found


def install_hooks(cfg: Config):
    path = _settings_path(cfg)
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError(f'"hooks" in {path} is not an object; not changing it.')
    ours = {"type": "command", "command": hook_command(), "timeout": 10}
    changed = False
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        present = False
        for group in groups:
            for i, h in enumerate((group or {}).get("hooks") or []):
                if _ours(h):
                    present = True
                    if h != ours:
                        group["hooks"][i] = dict(ours)
                        changed = True
        if not present:
            groups.append({"hooks": [dict(ours)]})
            changed = True
    if changed:
        _save(path, data)
    return changed


def uninstall_hooks(cfg: Config):
    path = _settings_path(cfg)
    data = _load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for ev in EVENTS:
        groups = hooks.get(ev)
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            inner = (group or {}).get("hooks") or []
            left = [h for h in inner if not _ours(h)]
            if len(left) != len(inner):
                changed = True
                if not left:
                    continue
                group = {**group, "hooks": left}
            kept.append(group)
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if not hooks:
        data.pop("hooks")
    if changed:
        _save(path, data)
    return changed


def enable_autostart(mode="tray"):
    """Start `sessionpulse tray` (panel icon) or `sessionpulse daemon` (no icon) at login."""
    icon = Path(__file__).with_name("icons") / "sessionpulse-idle.svg"
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Session Pulse\n"
        "Comment=Panel icon and dashboard for Claude Code sessions\n"
        f'Exec="{sys.executable}" -m sessionpulse {mode}\n'
        f"Icon={icon}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=5\n")
    return AUTOSTART


def disable_autostart():
    if AUTOSTART.exists():
        AUTOSTART.unlink()
        return True
    return False
