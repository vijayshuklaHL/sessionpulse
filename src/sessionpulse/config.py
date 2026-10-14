"""Settings, read from ~/.config/sessionpulse/config.toml when it exists.

Every key is optional; the defaults below apply otherwise. Example file:

    port = 8765
    refresh_seconds = 5
    claude_dir = "~/.claude"
    gap_minutes = 30
"""
import os
import secrets
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def _xdg(var, fallback):
    return Path(os.environ.get(var) or Path.home() / fallback)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "sessionpulse"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "sessionpulse"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def _default_claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"


@dataclass
class Config:
    port: int = 8765
    refresh_seconds: int = 5
    claude_dir: str = field(default_factory=_default_claude_dir)
    data_dir: str = str(DATA_DIR)
    # Active time: activity closer together than this counts as one block,
    # and a block is never shorter than min_block_minutes.
    gap_minutes: int = 30
    min_block_minutes: int = 15
    # Follow-ups: let Claude read quiet sessions and add their open items to your to-dos.
    # Off by default because it runs `claude -p`, which counts toward your usage.
    followups: bool = False
    followups_model: str = "haiku"
    followups_days: int = 30          # only sessions active in this many days
    followups_idle_minutes: int = 20  # wait until a session has been quiet this long
    followups_every_minutes: int = 5
    followups_batch: int = 5          # sessions read per round
    followups_parallel: int = 3       # sessions read at the same time
    claude_command: str = ""          # path to `claude`, if it isn't on PATH

    @property
    def claude(self) -> Path:
        return Path(self.claude_dir).expanduser()

    @property
    def data(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def events_file(self) -> Path:
        return self.data / "events.jsonl"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def load(path: Path = CONFIG_FILE) -> Config:
    cfg = Config()
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        known = {f.name for f in fields(Config)}
        for k, v in raw.items():
            if k in known:
                setattr(cfg, k, v)
    return cfg


def token(cfg: Config) -> str:
    """The dashboard's access token, created on first use (mode 600)."""
    path = cfg.data / "token"
    if path.exists():
        return path.read_text().strip()
    cfg.data.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok
