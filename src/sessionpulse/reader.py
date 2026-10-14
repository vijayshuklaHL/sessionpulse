"""Reads Claude Code's own files and turns them into one JSON-able snapshot.

Sources (all read-only):
  <claude_dir>/sessions/*.json        one file per running CLI process
  <claude_dir>/projects/*/*.jsonl     session transcripts
  <claude_dir>/projects/*/<id>/subagents/*.jsonl   subagent transcripts
  <data_dir>/events.jsonl             start/exit events written by our hook

None of these formats is documented by Anthropic, so every field is read
defensively and anything surprising is counted in snapshot["health"] instead
of raising. The dashboard shows those warnings so a Claude Code update that
changes a format is visible rather than silently producing wrong numbers.
"""
import json
import os
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import learn, todos
from .config import Config

TESTED_CLI = (2, 1)  # newest Claude Code minor version these parsers were checked against
LIVE_KEYS = ("pid", "sessionId", "status")

_cache = {}  # path -> (mtime_ns, size, summary)
lock = threading.Lock()


def local(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def is_noise(t):
    """Messages the CLI injects (command output, skill text, interrupts) rather than prompts."""
    t = t.strip()
    return (not t or t.startswith("<") or t.startswith("Caveat:")
            or t.startswith("Base directory for this skill")
            or "[Request interrupted" in t)


def active_hours(stamps, gap=timedelta(minutes=30), min_block=timedelta(minutes=15)):
    if not stamps:
        return 0.0
    stamps = sorted(stamps)
    total, start, prev = timedelta(), stamps[0], stamps[0]
    for t in stamps[1:]:
        if t - prev > gap:
            total += max(prev - start, min_block)
            start = t
        prev = t
    total += max(prev - start, min_block)
    return total.total_seconds() / 3600


def project_name(cwd):
    if not cwd:
        return "?"
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return os.path.relpath(cwd, home).replace(os.sep, "/")
    return cwd


def parse_transcript(path, main=True):
    """Summarise one transcript file.

    Token usage is de-duplicated by message id: the CLI writes one line per
    content block, and each line repeats the same usage object."""
    s = {"cwd": None, "first": None, "last": None, "prompts": 0, "first_prompt": "",
         "last_prompt": "", "models": defaultdict(int), "version": None, "title": None,
         "usage": defaultdict(int), "days": defaultdict(lambda: defaultdict(int)),
         "stamps": [], "tools": 0, "tool_names": defaultdict(int), "bad_lines": 0, "lines": 0, "usage_msgs": 0}
    seen = set()
    with open(path, errors="replace") as f:
        for line in f:
            s["lines"] += 1
            try:
                e = json.loads(line)
            except ValueError:
                s["bad_lines"] += 1
                continue
            if not isinstance(e, dict):
                s["bad_lines"] += 1
                continue
            typ = e.get("type")
            if main and typ in ("custom-title", "summary"):
                s["title"] = e.get("customTitle") or e.get("summary") or s["title"]
            if s["cwd"] is None and isinstance(e.get("cwd"), str):
                s["cwd"] = e["cwd"]
            v = e.get("version")
            if isinstance(v, str) and (_version_tuple(v) or ()) >= (_version_tuple(s["version"]) or ()):
                s["version"] = v  # a resumed session can span CLI upgrades; keep the newest
            try:
                t = local(e["timestamp"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            s["first"] = min(s["first"] or t, t)
            s["last"] = max(s["last"] or t, t)
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            if main and not e.get("isSidechain"):
                s["stamps"].append(t)
                if typ == "user" and msg.get("role") == "user":
                    body = text_of(msg.get("content"))
                    if not is_noise(body):
                        body = re.sub(r"\s+", " ", body).strip()
                        s["prompts"] += 1
                        s["first_prompt"] = s["first_prompt"] or body[:300]
                        s["last_prompt"] = body[:300]
            if typ != "assistant":
                continue
            content = msg.get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    s["tools"] += 1
                    if isinstance(c.get("name"), str):
                        s["tool_names"][c["name"]] += 1
            u = msg.get("usage")
            key = msg.get("id") or e.get("requestId") or e.get("uuid")
            if not isinstance(u, dict) or key in seen:
                continue
            seen.add(key)
            model = msg.get("model") or "unknown"
            if model.startswith("<"):  # synthetic messages, e.g. "<synthetic>"
                continue
            s["usage_msgs"] += 1
            vals = {"input": u.get("input_tokens") or 0,
                    "output": u.get("output_tokens") or 0,
                    "cache_write": u.get("cache_creation_input_tokens") or 0,
                    "cache_read": u.get("cache_read_input_tokens") or 0}
            s["models"][model] += vals["input"] + vals["output"]
            day = t.date().isoformat()
            for k, v in vals.items():
                if isinstance(v, (int, float)):
                    s["usage"][k] += v
                    s["days"][day][f"{model}|{k}"] += v
    return s


def _cached(path, main=True):
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        summ = parse_transcript(path, main)
    except OSError:
        return None
    _cache[path] = (st.st_mtime_ns, st.st_size, summ)
    return summ


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def read_live(claude: Path, health):
    live = {}
    d = claude / "sessions"
    if not d.is_dir():
        health["warnings"].append(f"{d} not found: live sessions can't be shown "
                                  "(older Claude Code, or a changed layout).")
        return live
    for p in d.glob("*.json"):
        try:
            j = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(j, dict) or any(k not in j for k in LIVE_KEYS):
            health["live_unrecognised"] += 1
            continue
        if not pid_alive(j.get("pid")):
            continue
        live[j["sessionId"]] = {
            "pid": j.get("pid"), "status": j.get("status"), "name": j.get("name"),
            "kind": j.get("kind"), "entrypoint": j.get("entrypoint"),
            "startedAt": j.get("startedAt"),
            "updatedAt": j.get("statusUpdatedAt") or j.get("updatedAt"),
        }
    return live


def read_events(path: Path):
    out = []
    if path.exists():
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("ts"):
                    out.append(e)
    return out


def _version_tuple(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except (AttributeError, ValueError):
        return None


def snapshot(cfg: Config):
    claude = cfg.claude
    gap = timedelta(minutes=cfg.gap_minutes)
    min_block = timedelta(minutes=cfg.min_block_minutes)
    health = {"warnings": [], "files": 0, "bad_lines": 0, "lines": 0,
              "live_unrecognised": 0, "newest_cli": None}
    sessions = {}
    proj_dir = claude / "projects"
    if not proj_dir.is_dir():
        health["warnings"].append(f"No transcripts at {proj_dir}. Is claude_dir right?")
    newest = None
    usage_msgs = 0
    for pdir in proj_dir.iterdir() if proj_dir.is_dir() else []:
        if not pdir.is_dir():
            continue
        for tf in pdir.glob("*.jsonl"):
            sid = tf.stem
            s = _cached(str(tf))
            if not s:
                continue
            health["files"] += 1
            health["bad_lines"] += s["bad_lines"]
            health["lines"] += s["lines"]
            usage_msgs += s["usage_msgs"]
            if not s["first"]:
                continue
            if s["version"] and (newest is None or
                                 (_version_tuple(s["version"]) or ()) > (_version_tuple(newest) or ())):
                newest = s["version"]
            usage = dict(s["usage"])
            days = {d: dict(v) for d, v in s["days"].items()}
            models = dict(s["models"])
            tool_names = dict(s["tool_names"])
            agents = 0
            for af in (pdir / sid / "subagents").glob("*.jsonl"):
                a = _cached(str(af), main=False)
                if not a:
                    continue
                agents += 1
                for k, v in a["usage"].items():
                    usage[k] = usage.get(k, 0) + v
                for m, v in a["models"].items():
                    models[m] = models.get(m, 0) + v
                for d, kv in a["days"].items():
                    dd = days.setdefault(d, {})
                    for k, v in kv.items():
                        dd[k] = dd.get(k, 0) + v
            if not s["prompts"] and not usage.get("output"):
                continue
            by_day = defaultdict(list)
            for t in s["stamps"]:
                by_day[t.date().isoformat()].append(t)
            sessions[sid] = {
                "id": sid, "cwd": s["cwd"], "project": project_name(s["cwd"]), "transcript": str(tf),
                "title": s["title"], "start": s["first"].isoformat(), "end": s["last"].isoformat(),
                "prompts": s["prompts"], "first_prompt": s["first_prompt"],
                "last_prompt": s["last_prompt"], "tools": s["tools"], "tool_names": tool_names,
                "agents": agents,
                "version": s["version"], "usage": usage, "days": days,
                "model": max(models, key=models.get) if models else None,
                "hours": round(active_hours(s["stamps"], gap, min_block), 2),
                "hours_by_day": {d: round(active_hours(v, gap, min_block), 2)
                                 for d, v in by_day.items()},
            }

    health["newest_cli"] = newest
    if health["lines"] and health["bad_lines"] / health["lines"] > 0.05:
        health["warnings"].append(f"{health['bad_lines']} of {health['lines']} transcript lines "
                                  "could not be read; the transcript format may have changed.")
    if health["files"] and sessions and not usage_msgs:
        health["warnings"].append("No token usage found in any transcript; "
                                  "the usage format may have changed.")
    if health["live_unrecognised"]:
        health["warnings"].append(f"{health['live_unrecognised']} live-session files had an "
                                  "unexpected shape and were skipped.")
    vt = _version_tuple(newest)
    if vt and vt[:2] > TESTED_CLI:
        health["warnings"].append(f"Claude Code {newest} is newer than the version Session Pulse "
                                  f"was tested with ({'.'.join(map(str, TESTED_CLI))}). "
                                  "Check the numbers look right.")

    live = read_live(claude, health)
    events = read_events(cfg.events_file)
    exits = {e.get("session"): e for e in events if e.get("event") == "SessionEnd"}
    for sid, s in sessions.items():
        if sid in live:
            s["state"] = live[sid]["status"] or "running"
            s["live"] = live[sid]
        elif sid in exits:
            s["state"] = "exited"
            s["exit_reason"] = exits[sid].get("reason")
            s["exit_at"] = exits[sid].get("ts")
        else:
            s["state"] = "closed"
    for sid, l in live.items():  # just started; no transcript written yet
        if sid not in sessions:
            sessions[sid] = {"id": sid, "project": "?", "state": l["status"] or "running",
                             "live": l, "start": None, "end": None, "prompts": 0,
                             "usage": {}, "days": {}, "hours": 0, "hours_by_day": {}}
    for e in events:
        s = sessions.get(e.get("session"))
        e["project"] = s["project"] if s else project_name(e.get("cwd"))
    sessions = list(sessions.values())
    return {"now": datetime.now().astimezone().isoformat(), "refresh": cfg.refresh_seconds,
            "sessions": sessions, "events": events[-300:], "health": health,
            "todos": todos.load(cfg), "learn": learn.suggest(cfg, sessions)}
