"""Your own to-do list, kept in <data_dir>/todos.json.

Items come from you (dashboard or `sessionpulse todo`) or from your sessions
(followups.py, marked source="session"). Ticking an item stamps `done_at`;
that is what the dashboard's Completed section shows.
"""
import json
import os
import re
import secrets
import threading
from datetime import datetime

MAX_TEXT = 500
KINDS = ("external", "checklist", "decision", "waiting", "task")
STATUSES = ("hold", "archived")  # parked items: not done, but not on the active list either
lock = threading.Lock()


def _path(cfg):
    return cfg.data / "todos.json"


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load(cfg):
    try:
        items = json.loads(_path(cfg).read_text())
    except (OSError, ValueError):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("id") and i.get("text")] \
        if isinstance(items, list) else []


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _save(cfg, items):
    _write(_path(cfg), items)


def is_open(item):
    """Still to do: not done, on hold or archived."""
    return not item.get("done_at") and item.get("status") not in STATUSES


def _dismissed_path(cfg):
    return cfg.data / "todos-dismissed.json"


def dismissed(cfg):
    """Follow-ups you deleted, so they aren't picked up again."""
    try:
        d = json.loads(_dismissed_path(cfg).read_text())
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


# Secrets that may turn up in transcripts; replaced before anything is stored or shown.
_SECRET = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
                     r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                     r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
                     r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
# `password=x`, or `password: x` when x has a digit in it (so "the token: it leaked" stays as it is)
_SECRET_VALUE = re.compile(r"(?i)\b([a-z0-9_]*(?:password|passwd|secret|token|api_?key)[a-z0-9_]*"
                           r"(?:\s*=\s*|:\s*(?=\S*\d)))(?!\[secret\])\S+")


def redact(text):
    """Text with API keys, tokens and `password=...` style values replaced by [secret]."""
    return _SECRET_VALUE.sub(r"\1[secret]", _SECRET.sub("[secret]", text))


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


SIMILAR = 0.6   # word overlap at which two to-dos count as the same one reworded
RECENT_DAYS = 30
_STOP = frozenset("a an the and or of to in on for with from by at as is be it its this that into via "
                  "per their your our my all any each then than not no do get set up out off whether".split())


def _words(text):
    """The content words of a to-do, with a plain plural 's' dropped."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def similar(a, b):
    """True if two to-do texts say the same thing in different words."""
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= SIMILAR


def _recent(stamp, days=RECENT_DAYS):
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(stamp)).days < days
    except (TypeError, ValueError):
        return False


def merge_followups(cfg, sess, found, resolved):
    """Add follow-ups found in a session and close the ones it resolved.

    Returns (added, closed). Skips anything already on the list or dismissed,
    including the same to-do reworded (see similar()) within the project."""
    added = closed = 0
    project = sess.get("project")
    why = {r["id"]: r.get("evidence") for r in resolved if isinstance(r, dict) and r.get("id")}
    resolved = set(why) | {r for r in resolved if isinstance(r, str)}
    with lock:
        items = load(cfg)
        gone = dismissed(cfg)
        seen = {_norm(i["text"]) for i in items} | {_norm(d.get("text", "")) for d in gone}
        # reworded copies: compare with this project's live items, recently done ones and dismissals
        near = [i["text"] for i in items if i.get("project") == project
                and (not i.get("done_at") or _recent(i["done_at"]))]
        near += [d.get("text", "") for d in gone if d.get("project") == project]
        for f in found:
            text = redact(_clean(f.get("text") if isinstance(f, dict) else "", 200))
            if not text or _norm(text) in seen or any(similar(text, t) for t in near):
                continue
            seen.add(_norm(text))
            near.append(text)
            kind = f.get("kind") if f.get("kind") in KINDS else "task"
            item = {"id": secrets.token_hex(4), "text": text, "project": sess.get("project"),
                    "created": _now(), "done_at": None, "source": "session", "kind": kind,
                    "detail": redact(_clean(f.get("detail"), 300)) or None,
                    "session": sess.get("id"), "cwd": sess.get("cwd"),
                    "session_at": sess.get("end")}
            if isinstance(f.get("evidence"), dict):
                item["evidence"] = f["evidence"]
            items.append(item)
            added += 1
        for i in items:
            if i["id"] in resolved and not i.get("done_at") and i.get("project") == project \
                    and sess.get("id") not in i.get("not_closed_by", ()):
                i["done_at"] = _now()
                i["closed_by"] = sess.get("id")
                if isinstance(why.get(i["id"]), dict):
                    i["closed_evidence"] = why[i["id"]]
                closed += 1
        if added or closed:
            _save(cfg, items)
    return added, closed


def merge_duplicates(cfg, groups):
    """Archive all but one open item of each group of duplicate ids; returns how many.

    The kept item is the one in progress, else the newest. Archived copies record
    `duplicate_of`, so they stay on the board and can be moved back."""
    merged = 0
    with lock:
        items = load(cfg)
        by_id = {i["id"]: i for i in items}
        for group in groups:
            live = [by_id[g] for g in dict.fromkeys(group) if g in by_id and is_open(by_id[g])]
            if len(live) < 2:
                continue
            keep = max(live, key=lambda i: (bool(i.get("doing")), i.get("created") or ""))
            for i in live:
                if i is not keep:
                    i["status"] = "archived"
                    i["doing"] = False
                    i["duplicate_of"] = keep["id"]
                    merged += 1
        if merged:
            _save(cfg, items)
    return merged


def _clean(text, limit=MAX_TEXT):
    return " ".join(str(text or "").split())[:limit]


def _reopen(item):
    """Un-close an item. If a session closed it, that session may not close it again."""
    if item.get("closed_by"):
        item["not_closed_by"] = [*item.get("not_closed_by", []), item["closed_by"]][-10:]
    item.pop("closed_by", None)
    item.pop("closed_evidence", None)


def apply(cfg, op):
    """Run one change and return the new list.

    op is a dict: {"op": "add", "text", "project"?} | {"op": "done"|"undo"|"delete", "id"}
    | {"op": "delete", "ids": [...]} | {"op": "set", "id", "kind"?, "doing"?, "done"?, "status"?}
    | {"op": "edit", "id", "text"}. Raises ValueError on anything else."""
    if not isinstance(op, dict):
        raise ValueError("expected an object")
    kind = op.get("op")
    with lock:
        items = load(cfg)
        if kind == "add":
            text = _clean(op.get("text"))
            if not text:
                raise ValueError("text is empty")
            items.append({"id": secrets.token_hex(4), "text": text,
                          "project": _clean(op.get("project"), 200) or None,
                          "created": _now(), "done_at": None})
        elif kind == "delete" and isinstance(op.get("ids"), list):
            ids = set(map(str, op["ids"]))
            gone = [i for i in items if i["id"] in ids]
            items = [i for i in items if i["id"] not in ids]
            d = dismissed(cfg)
            d += [{"text": i["text"], "project": i.get("project"), "at": _now()}
                  for i in gone if i.get("source") == "session"]
            _write(_dismissed_path(cfg), d[-2000:])
        elif kind in ("done", "undo", "delete", "edit", "set"):
            hit = next((i for i in items if i["id"] == op.get("id")), None)
            if hit is None:
                raise ValueError("no such item")
            if kind == "set":  # the board: move a card between columns
                if "kind" in op:
                    if op["kind"] not in (*KINDS, None, ""):
                        raise ValueError("unknown kind")
                    hit["kind"] = op["kind"] or None
                if "doing" in op:
                    hit["doing"] = bool(op["doing"])
                if "status" in op:
                    if op["status"] not in (*STATUSES, None, ""):
                        raise ValueError("unknown status")
                    if op["status"]:
                        hit["status"] = op["status"]
                        hit["doing"] = False
                    else:
                        hit.pop("status", None)
                        hit.pop("duplicate_of", None)
                if "done" in op:
                    hit["done_at"] = (hit.get("done_at") or _now()) if op["done"] else None
                    if not op["done"]:
                        _reopen(hit)
                if hit.get("done_at"):
                    hit["doing"] = False
                    hit.pop("status", None)
            elif kind == "done":
                hit["done_at"] = hit.get("done_at") or _now()
                hit["doing"] = False
                hit.pop("status", None)
            elif kind == "undo":
                hit["done_at"] = None
                _reopen(hit)
            elif kind == "delete":
                items.remove(hit)
                if hit.get("source") == "session":  # remember it, so it isn't picked up again
                    d = dismissed(cfg)[-2000:]
                    d.append({"text": hit["text"], "project": hit.get("project"), "at": _now()})
                    _write(_dismissed_path(cfg), d)
            else:
                text = _clean(op.get("text"))
                if not text:
                    raise ValueError("text is empty")
                hit["text"] = text
        else:
            raise ValueError(f"unknown op {kind!r}")
        _save(cfg, items)
        return items
