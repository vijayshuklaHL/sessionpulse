"""Follow-ups: to-dos picked out of your sessions automatically.

Sessions often end with work that can't be finished in them: something to do
outside (call a vendor, check a site), an audit checklist, a decision you need
to take, an answer you're waiting for. This module asks Claude to read a quiet
session and list exactly those, then adds them to your to-do list, linked to the
session. It also closes earlier follow-ups that a later session shows are done.

It is off unless config.toml has `followups = true`, because it runs
`claude -p` (counts toward your Claude usage) on your transcripts. Each run uses
--no-session-persistence and disables all hooks, so it doesn't show up as a
session, in the dashboard or in any timesheet hook of yours.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from . import reader, todos

KINDS = todos.KINDS
DIGEST_CHARS = 60000   # how much of a transcript goes to the model
USER_CHARS = 1200      # per prompt
CLAUDE_CHARS = 2500    # per reply
MAX_ITEMS = 12
MAX_OPEN = 150         # open to-dos shown to the model per project
TIMEOUT = 180
BACKOFF = (timedelta(hours=1), timedelta(hours=6))  # wait after a session's 1st and 2nd failure;
                                                    # after the 3rd, wait until its transcript grows
USAGE_DAYS = 90
# Errors that are nobody's fault in particular: no session is marked as failing for these.
NOT_READY = re.compile(r"usage limit|limit reached|rate.?limit|overloaded|credit balance|/login|api key|"
                       r"authenticat|network|econn|etimedout|fetch failed|\b(429|5\d\d)\b", re.I)

SYSTEM = """You read a transcript of a Claude Code session between a user and Claude and list the \
follow-ups the user still has open when the session went quiet. Reply only with the JSON the schema asks for.

A follow-up is something real and specific that was NOT finished in the session:
- external: work outside this session (contact a person or vendor, visit or check something physically, \
log in somewhere, send or sign something, buy or renew something)
- checklist: audit, review or verification items the user has to go through
- decision: a choice the user was asked to make, or deferred ("decide later", "waiting on you")
- waiting: an answer, document or action expected from someone else
- task: work to do in a later session (it was planned or promised but not done)

Do not list: things completed in the session; Claude's own intermediate steps; vague ideas; general \
advice; anything the user explicitly rejected. Merge duplicates. If nothing is open, return no items.

Write each item as a short imperative the user would put on their own list (max 110 characters), in \
English, naming the concrete thing ("Ask the vendor for the September invoice broken down by team"). \
Put the reason or context in `detail` (max 200 characters). Never copy passwords, keys or tokens into items.

You also get the user's currently open to-dos for this project, with ids. Put an id in `resolved` only \
if this transcript clearly shows that item was completed. Do not repeat an open to-do as a new item, even \
in different words: if an open to-do already covers the work, leave it out. Do not re-add anything listed \
under "dismissed"."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "maxItems": MAX_ITEMS, "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
                           "detail": {"type": "string"}},
            "required": ["text", "kind"]}},
        "resolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items", "resolved"],
}

_state_lock = threading.Lock()
_usage_lock = threading.Lock()
status = {"running": False, "last_run": None, "last_error": None, "done": 0}


class NotReady(RuntimeError):
    """The model can't be used right now (usage limit, outage, signed out): try again later."""


# ---------- transcript digest ----------
def digest(path, project="", limit=DIGEST_CHARS):
    """The user prompts and Claude's prose replies, oldest first, trimmed to fit."""
    parts = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("isSidechain") or e.get("type") not in ("user", "assistant"):
                continue
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            body = reader.text_of(msg.get("content")).strip()
            if not body or (e["type"] == "user" and reader.is_noise(body)):
                continue
            body = re.sub(r"\n{3,}", "\n\n", body)
            cap = USER_CHARS if e["type"] == "user" else CLAUDE_CHARS
            if len(body) > cap:
                body = body[:cap // 2] + "\n[…]\n" + body[-cap // 2:]
            when = str(e.get("timestamp", ""))[:16].replace("T", " ")
            parts.append(f"### {'USER' if e['type'] == 'user' else 'CLAUDE'} {when}\n{body}")
    text = "\n\n".join(parts)
    if len(text) > limit:  # keep the opening request and the end, where open items get summed up
        head = parts[0][:3000] if parts else ""
        text = head + "\n\n[… earlier part of the session left out …]\n\n" + text[-(limit - len(head)):]
    return f"Project: {project}\n\n{text}"


# ---------- running claude ----------
def claude_bin(cfg):
    for c in (cfg.claude_command, shutil.which("claude"), str(Path.home() / ".local/bin/claude"),
              str(Path.home() / ".claude/local/claude")):
        if c and (os.sep not in c or Path(c).is_file()):
            found = shutil.which(c) if os.sep not in c else c
            if found:
                return found
    return None


def ask_claude(cfg, prompt, system=SYSTEM, schema=SCHEMA, usage=None):
    """Run `claude -p` and return its structured output. What the call used is added to the daily
    totals and, if given, to the `usage` dict."""
    exe = claude_bin(cfg)
    if not exe:
        raise RuntimeError("the `claude` command was not found; set claude_command in config.toml")
    work = cfg.data / "followups-cwd"
    work.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-p", "--model", cfg.followups_model, "--no-session-persistence", "--tools", "",
           "--settings", json.dumps({"disableAllHooks": True}), "--system-prompt", system,
           "--output-format", "json", "--json-schema", json.dumps(schema)]
    env = {**os.environ, "SESSIONPULSE_CHILD": "1"}
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=work, env=env, timeout=TIMEOUT)
    try:
        out = json.loads(r.stdout)
        if not isinstance(out, dict):
            raise ValueError
    except ValueError:
        _fail(f"claude exited {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}")
    record_usage(cfg, out, usage)
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        _fail(f"claude returned an error: {str(out.get('result'))[:300]}")
    return out["structured_output"]


def _fail(msg):
    raise (NotReady if NOT_READY.search(msg) else RuntimeError)(msg)


# ---------- model usage ----------
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _num(v, kind=int):
    try:
        return max(kind(v or 0), 0)
    except (TypeError, ValueError):
        return 0


def _usage_path(cfg):
    return cfg.data / "followups-usage.json"


def load_usage(cfg):
    """Model use per day: {"2026-10-29": {"calls": 3, "input_tokens": ..., "cost_usd": ...}}."""
    try:
        u = json.loads(_usage_path(cfg).read_text())
        return u if isinstance(u, dict) else {}
    except (OSError, ValueError):
        return {}


def record_usage(cfg, out, into=None):
    u = out.get("usage") if isinstance(out.get("usage"), dict) else {}
    run = {"calls": 1, **{k: _num(u.get(k)) for k in USAGE_KEYS},
           "cost_usd": _num(out.get("total_cost_usd"), float), "ms": _num(out.get("duration_ms"))}
    if into is not None:
        for k, v in run.items():
            into[k] = into.get(k, 0) + v
    cfg.data.mkdir(parents=True, exist_ok=True)
    with _usage_lock, open(cfg.data / "followups-usage.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # the tray's worker and the CLI may both be counting
        days = load_usage(cfg)
        today = days.setdefault(date.today().isoformat(), {})
        for k, v in run.items():
            today[k] = round(_num(today.get(k), type(v)) + v, 6)
        days = dict(sorted(days.items())[-USAGE_DAYS:])
        tmp = _usage_path(cfg).with_suffix(".tmp")
        tmp.write_text(json.dumps(days, indent=1))
        os.replace(tmp, _usage_path(cfg))


def usage_totals(cfg, days):
    """Model use over the last `days` days, with `tokens` = everything read and written."""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    tot = dict.fromkeys(("calls", *USAGE_KEYS, "ms"), 0) | {"cost_usd": 0.0}
    for day, u in load_usage(cfg).items():
        if day >= since and isinstance(u, dict):
            for k in tot:
                tot[k] += _num(u.get(k), type(tot[k]))
    tot["tokens"] = sum(tot[k] for k in USAGE_KEYS)
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    return tot


# ---------- state ----------
def _state_path(cfg):
    return cfg.data / "followups.json"


def load_state(cfg):
    try:
        st = json.loads(_state_path(cfg).read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def failing(cfg):
    """Sessions whose last read failed: [(id, entry)], most failures first."""
    return sorted(((sid, e) for sid, e in load_state(cfg).items() if isinstance(e, dict) and e.get("fails")),
                  key=lambda x: -x[1]["fails"])


def _failed(prev, sess, size, exc, now):
    """The state entry after a failed read: back off, then wait for the transcript to grow."""
    fails = _num(prev.get("fails")) + 1
    e = {**prev, "project": sess.get("project"), "fails": fails, "failed_size": size,
         "failed_at": now.isoformat(timespec="seconds"), "last_error": str(exc)[:300]}
    e.pop("next_try", None)
    if fails <= len(BACKOFF):
        e["next_try"] = (now + BACKOFF[fails - 1]).isoformat(timespec="seconds")
    return e


def _waiting(e, size, now):
    """True while a failed session should be left alone."""
    if not e.get("fails") or e.get("failed_size") != size:
        return False  # never failed, or it has grown since: worth another try
    try:
        return now < datetime.fromisoformat(e["next_try"])
    except (KeyError, TypeError, ValueError):
        return True  # out of retries until it grows


def _save_state(cfg, st):
    cfg.data.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, _state_path(cfg))


# ---------- one session ----------
def checked(out, shown):
    """The model's reply with anything malformed dropped, and `resolved` limited to ids it was shown."""
    items = out.get("items") if isinstance(out.get("items"), list) else []
    items = [f for f in items if isinstance(f, dict) and isinstance(f.get("text"), str) and f["text"].strip()]
    resolved = out.get("resolved") if isinstance(out.get("resolved"), list) else []
    return items[:MAX_ITEMS], [r for r in dict.fromkeys(resolved) if isinstance(r, str) and r in shown]


def process(cfg, sess, path, usage=None):
    """Extract follow-ups from one session and merge them into the to-do list."""
    items = todos.load(cfg)
    project = sess.get("project") or ""
    # newest first: the model must see recent items, or it adds them again reworded
    open_ = sorted((i for i in items if not i.get("done_at") and i.get("status") != "archived"
                    and i.get("project") == project),
                   key=lambda i: i.get("created") or "", reverse=True)[:MAX_OPEN]
    dismissed = [d for d in todos.dismissed(cfg) if d.get("project") == project][-40:]
    prompt = digest(path, project)
    prompt += "\n\n## Open to-dos for this project\n" + (
        "\n".join(f"- [{i['id']}] {i['text']}" for i in open_) or "(none)")
    prompt += "\n\n## Dismissed (never re-add)\n" + (
        "\n".join(f"- {d['text']}" for d in dismissed) or "(none)")
    found, resolved = checked(ask_claude(cfg, prompt, usage=usage), {i["id"] for i in open_})
    return todos.merge_followups(cfg, sess, found, resolved)


# ---------- one-off cleanup ----------
DEDUPE_SYSTEM = """You get one project's open to-do list, one item per line with its id. Group the items \
that are the same piece of work written in different words. Reply only with the JSON the schema asks for.

Two items are duplicates only if doing one would finish the other. Items about different devices, sites, \
people, dates or steps are NOT duplicates, even if worded alike ("Disable Telnet on the routers" and \
"Disable WAN management on the routers" are separate). A broader item does not swallow a narrower one \
unless it names that same work. Leave out every item that has no duplicate. When unsure, leave it out."""

DEDUPE_SCHEMA = {
    "type": "object",
    "properties": {"groups": {"type": "array", "items": {
        "type": "array", "minItems": 2, "items": {"type": "string"}}}},
    "required": ["groups"],
}


def find_duplicates(cfg, project):
    """Ask the model which open to-dos in one project are reworded copies; returns groups of ids."""
    open_ = [i for i in todos.load(cfg) if todos.is_open(i) and (i.get("project") or "") == (project or "")]
    if len(open_) < 2:
        return []
    ids = {i["id"] for i in open_}
    out = ask_claude(cfg, "\n".join(f"[{i['id']}] {i['text']}" for i in open_),
                     system=DEDUPE_SYSTEM, schema=DEDUPE_SCHEMA)
    groups = []
    for g in out.get("groups") or []:
        g = [x for x in dict.fromkeys(map(str, g)) if x in ids]
        if len(g) > 1:
            groups.append(g)
    return groups


def candidates(cfg, snap, days=None, idle_minutes=None, force=False):
    """Sessions worth (re)reading: recent, quiet for a while, and changed since last read."""
    days = cfg.followups_days if days is None else days
    idle = timedelta(minutes=cfg.followups_idle_minutes if idle_minutes is None else idle_minutes)
    now = datetime.now().astimezone()
    st = load_state(cfg)
    out = []
    for s in snap["sessions"]:
        if not s.get("end") or not s.get("prompts") or not s.get("transcript"):
            continue
        end = datetime.fromisoformat(s["end"])
        if now - end > timedelta(days=days) or now - end < idle or s.get("state") == "busy":
            continue
        try:
            size = os.stat(s["transcript"]).st_size
        except OSError:
            continue
        e = st.get(s["id"]) if isinstance(st.get(s["id"]), dict) else {}
        if not force and (e.get("size") == size or _waiting(e, size, now)):
            continue
        out.append((s, size))
    out.sort(key=lambda x: x[0]["end"])  # oldest first, so later sessions can close earlier items
    return out


class Busy(RuntimeError):
    pass


def scan(cfg, limit=None, days=None, idle_minutes=None, session=None, force=False, log=None):
    """Read the sessions that need it. Returns (sessions read, items added, items closed).

    Only one scan runs at a time across processes (the tray's worker and the CLI)."""
    cfg.data.mkdir(parents=True, exist_ok=True)
    with open(cfg.data / "followups.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("another follow-up scan is running; try again when it finishes")
        return _scan(cfg, limit, days, idle_minutes, session, force, log)


def _scan(cfg, limit, days, idle_minutes, session, force, log):
    if not claude_bin(cfg):  # not the sessions' fault, so don't mark them as failing
        raise RuntimeError("the `claude` command was not found; set claude_command in config.toml")
    with reader.lock:
        snap = reader.snapshot(cfg)
    todo = candidates(cfg, snap, days, idle_minutes, force or bool(session))
    if session:
        todo = [x for x in todo if x[0]["id"].startswith(session)]
    if limit:
        todo = todo[-limit:]
    added = closed = 0
    errors = []
    halt = threading.Event()

    def one(job):
        n, (s, size) = job
        if halt.is_set():
            return 0, 0
        use = {}
        try:
            a, c = process(cfg, s, s["transcript"], use)
        except NotReady as exc:  # a usage limit or outage: stop the round, blame no session
            halt.set()
            errors.append(str(exc))
            if log:
                log(f"[{n}/{len(todo)}] stopping: {str(exc)[:160]}")
            return 0, 0
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
            with _state_lock:
                st = load_state(cfg)
                prev = st.get(s["id"]) if isinstance(st.get(s["id"]), dict) else {}
                e = st[s["id"]] = _failed(prev, s, size, exc, datetime.now().astimezone())
                _save_state(cfg, st)
            if log:
                when = f"next try after {e['next_try'][11:16]}" if e.get("next_try") else \
                    "not retried until the session grows"
                log(f"[{n}/{len(todo)}] {s['project']}: failed ({e['fails']}x, {when}): {str(exc)[:120]}")
            return 0, 0
        with _state_lock:
            st = load_state(cfg)
            st[s["id"]] = {"size": size, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                           "project": s.get("project"), "added": a, "closed": c,
                           "tokens": sum(use.get(k, 0) for k in USAGE_KEYS),
                           "cost_usd": round(use.get("cost_usd", 0), 6)}
            _save_state(cfg, st)
        if log:
            log(f"[{n}/{len(todo)}] {s['project']} · {s['end'][:16]} · +{a} new, {c} closed")
        return a, c

    # A few at a time: each request mostly waits on the model. Oldest first, so a later
    # session usually gets to see (and close) what an earlier one left open.
    with ThreadPoolExecutor(max_workers=max(1, cfg.followups_parallel)) as pool:
        for a, c in pool.map(one, enumerate(todo, 1)):
            added += a
            closed += c
    if halt.is_set():
        raise NotReady(errors[-1])
    if errors and len(errors) == len(todo):
        raise RuntimeError(errors[-1])
    return len(todo), added, closed


# ---------- background worker ----------
def start_worker(cfg):
    if not cfg.followups or os.environ.get("SESSIONPULSE_CHILD"):
        return None

    def loop():
        time.sleep(30)
        while True:
            status["running"] = True
            try:
                status["done"] += scan(cfg, limit=cfg.followups_batch)[0]
                status["last_error"] = None
                status["last_run"] = datetime.now().astimezone().isoformat(timespec="seconds")
            except Busy:
                pass  # `sessionpulse followups` is running; it covers the same sessions
            except Exception as exc:  # keep the worker alive; show the problem on the dashboard
                status["last_error"] = str(exc)[:300]
            status["running"] = False
            time.sleep(cfg.followups_every_minutes * 60)

    t = threading.Thread(target=loop, daemon=True, name="followups")
    t.start()
    return t
