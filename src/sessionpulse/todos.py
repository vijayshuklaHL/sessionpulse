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


def _dismissed_path(cfg):
    return cfg.data / "todos-dismissed.json"


def dismissed(cfg):
    """Follow-ups you deleted, so they aren't picked up again."""
    try:
        d = json.loads(_dismissed_path(cfg).read_text())
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def merge_followups(cfg, sess, found, resolved):
    """Add follow-ups found in a session and close the ones it resolved.

    Returns (added, closed). Skips anything already on the list or dismissed."""
    added = closed = 0
    with lock:
        items = load(cfg)
        seen = {_norm(i["text"]) for i in items} | {_norm(d.get("text", "")) for d in dismissed(cfg)}
        for f in found:
            text = _clean(f.get("text") if isinstance(f, dict) else "", 200)
            if not text or _norm(text) in seen:
                continue
            seen.add(_norm(text))
            kind = f.get("kind") if f.get("kind") in KINDS else "task"
            items.append({"id": secrets.token_hex(4), "text": text, "project": sess.get("project"),
                          "created": _now(), "done_at": None, "source": "session", "kind": kind,
                          "detail": _clean(f.get("detail"), 300) or None,
                          "session": sess.get("id"), "cwd": sess.get("cwd"),
                          "session_at": sess.get("end")})
            added += 1
        for i in items:
            if i["id"] in resolved and not i.get("done_at") and i.get("project") == sess.get("project"):
                i["done_at"] = _now()
                i["closed_by"] = sess.get("id")
                closed += 1
        if added or closed:
            _save(cfg, items)
    return added, closed


def _clean(text, limit=MAX_TEXT):
    return " ".join(str(text or "").split())[:limit]


def apply(cfg, op):
    """Run one change and return the new list.

    op is a dict: {"op": "add", "text", "project"?} | {"op": "done"|"undo"|"delete", "id"}
    | {"op": "delete", "ids": [...]} | {"op": "set", "id", "kind"?, "doing"?, "done"?}
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
                if "done" in op:
                    hit["done_at"] = (hit.get("done_at") or _now()) if op["done"] else None
                    if not op["done"]:
                        hit.pop("closed_by", None)
                if hit.get("done_at"):
                    hit["doing"] = False
            elif kind == "done":
                hit["done_at"] = hit.get("done_at") or _now()
                hit["doing"] = False
            elif kind == "undo":
                hit["done_at"] = None
                hit.pop("closed_by", None)
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
