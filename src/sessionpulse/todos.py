"""Your own to-do list, kept in <data_dir>/todos.json.

Items come from you (dashboard or `sessionpulse todo`) or from your sessions
(followups.py, marked source="session"). Ticking an item stamps `done_at`;
that is what the dashboard's Completed section shows.
"""
import csv
import io
import json
import os
import re
import secrets
import threading
from datetime import datetime

MAX_TEXT = 500
MAX_DETAIL = 600
KINDS = ("external", "checklist", "decision", "waiting", "task")
STATUSES = ("hold", "archived")  # parked items: not done, but not on the active list either
REASONS = ("not_useful", "wrong_project")  # why a follow-up was dismissed (optional)
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


def similar(a, b, at=SIMILAR, shared=1):
    """True if two to-do texts say the same thing in different words: their content words
    overlap by at least `at`, and they share at least `shared` of them."""
    wa, wb = _words(a), _words(b)
    both = len(wa & wb)
    return bool(wa and wb) and both >= shared and both / len(wa | wb) >= at


# Across projects the same work tends to be worded more differently, and a false match costs a
# click ("Not the same") rather than a lost item, so a looser test only flags the pair.
CROSS = 0.4
CROSS_SHARED = 3


def _links_path(cfg):
    return cfg.data / "todos-links.json"


def links(cfg):
    """{"not_same": [[id, id], ...], "related": [[project, project], ...]}, each pair sorted."""
    try:
        d = json.loads(_links_path(cfg).read_text())
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    return {k: [sorted(map(str, p)) for p in d.get(k, ()) if isinstance(p, list) and len(p) == 2]
            for k in ("not_same", "related")}


def _link(cfg, kind, a, b):
    d = links(cfg)
    pair = sorted((str(a), str(b)))
    if a != b and pair not in d[kind]:
        d[kind] = (d[kind] + [pair])[-2000:]
        _write(_links_path(cfg), d)


def related(cfg, project):
    """Projects linked to this one by a merge you confirmed: the same work can be tracked in either."""
    return {p for pair in links(cfg)["related"] if project in pair for p in pair if p != project}


def maybe_same(item, items, not_same=()):
    """The open item in another project that this one looks like a copy of, or None."""
    apart = {tuple(p) for p in not_same}
    hits = [i for i in items if is_open(i) and i["id"] != item["id"]
            and (i.get("project") or "") != (item.get("project") or "")
            and tuple(sorted((i["id"], item["id"]))) not in apart
            and similar(item["text"], i["text"], CROSS, CROSS_SHARED)]
    return max(hits, key=lambda i: i.get("created") or "", default=None)


def _recent(stamp, days=RECENT_DAYS):
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(stamp)).days < days
    except (TypeError, ValueError):
        return False


def merge_followups(cfg, sess, found, resolved):
    """Add follow-ups found in a session and close the ones it resolved.

    Returns (added, closed). Skips anything already on the list or dismissed,
    including the same to-do reworded (see similar()) within the project. A found item
    with `after` (the id of an open item it waits on) goes into that item's detail."""
    added = closed = noted = 0
    project = sess.get("project")
    here = {project} | related(cfg, project)  # closing reaches projects you linked to this one
    not_same = links(cfg)["not_same"]
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
        by_id = {i["id"]: i for i in items}
        for f in found:
            text = redact(_clean(f.get("text") if isinstance(f, dict) else "", 200))
            first = by_id.get(f.get("after")) if isinstance(f, dict) else None
            if text and first and is_open(first):  # a step that waits on an open item: add it to that item's notes
                then = f"Then: {text}"
                if _norm(then) not in _norm(first.get("detail") or ""):
                    first["detail"] = _clean(f"{first.get('detail') or ''} {then}", MAX_DETAIL)
                    noted += 1
                continue
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
            twin = maybe_same(item, items, not_same)
            if twin:
                item["possible_duplicate_of"] = twin["id"]
            items.append(item)
            added += 1
        for i in items:
            if i["id"] in resolved and not i.get("done_at") and i.get("project") in here \
                    and sess.get("id") not in i.get("not_closed_by", ()):
                i["done_at"] = _now()
                i["closed_by"] = sess.get("id")
                if isinstance(why.get(i["id"]), dict):
                    i["closed_evidence"] = why[i["id"]]
                closed += 1
        if added or closed or noted:
            _save(cfg, items)
    return added, closed


def merge_duplicates(cfg, groups, link=False):
    """Archive all but one open item of each group of duplicate ids; returns how many.

    The kept item is the one in progress, else the newest. Archived copies record
    `duplicate_of`, so they stay on the board and can be moved back. With `link`, the
    projects of a merged group become related (see related())."""
    merged = 0
    pairs = set()
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
                    _archive_copy(i, keep)
                    merged += 1
                    if i.get("project") and keep.get("project") and i["project"] != keep["project"]:
                        pairs.add((i["project"], keep["project"]))
        if merged:
            _save(cfg, items)
        if link:
            for a, b in pairs:
                _link(cfg, "related", a, b)
    return merged


def _archive_copy(item, keep):
    item["status"] = "archived"
    item["doing"] = False
    item["duplicate_of"] = keep["id"]
    item.pop("possible_duplicate_of", None)


def _clean(text, limit=MAX_TEXT):
    return " ".join(str(text or "").split())[:limit]


def _reopen(item):
    """Un-close an item. If a session closed it, that session may not close it again."""
    if item.get("closed_by"):
        item["not_closed_by"] = [*item.get("not_closed_by", []), item["closed_by"]][-10:]
    item.pop("closed_by", None)
    item.pop("closed_evidence", None)


def _dismissal(item, reason=None):
    d = {"text": item["text"], "project": item.get("project"), "at": _now()}
    if item.get("kind"):
        d["kind"] = item["kind"]
    if reason:
        d["reason"] = reason
    return d


def apply(cfg, op):
    """Run one change and return the new list.

    op is a dict: {"op": "add", "text", "project"?} | {"op": "done"|"undo", "id"}
    | {"op": "delete", "id", "reason"?} (reason: one of REASONS, kept with the dismissal)
    | {"op": "delete", "ids": [...]} | {"op": "set", "id", "kind"?, "doing"?, "done"?, "status"?}
    | {"op": "edit", "id", "text"} | {"op": "merge"|"not_same", "id"} (an item flagged as
    `possible_duplicate_of` another). Raises ValueError on anything else."""
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
            d += [_dismissal(i) for i in gone if i.get("source") == "session"]
            _write(_dismissed_path(cfg), d[-2000:])
        elif kind in ("done", "undo", "delete", "edit", "set", "merge", "not_same"):
            hit = next((i for i in items if i["id"] == op.get("id")), None)
            if hit is None:
                raise ValueError("no such item")
            if op.get("reason") not in (*REASONS, None, ""):
                raise ValueError("unknown reason")
            if kind in ("merge", "not_same"):
                twin = next((i for i in items if i["id"] == hit.get("possible_duplicate_of")), None)
                hit.pop("possible_duplicate_of", None)
                if twin is None:
                    pass  # the other one is gone: nothing left to compare with
                elif kind == "not_same":
                    _link(cfg, "not_same", hit["id"], twin["id"])
                else:  # keep the older one, which may already be in progress
                    _archive_copy(hit, twin)
                    if hit.get("project") and twin.get("project") and hit["project"] != twin["project"]:
                        _link(cfg, "related", hit["project"], twin["project"])
            elif kind == "set":  # the board: move a card between columns
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
                    d.append(_dismissal(hit, op.get("reason")))
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


def kind_stats(cfg):
    """Follow-ups per kind: {kind: {"added", "open", "done", "parked", "not_useful", "dismissed"}}.
    Dismissals from before kinds were kept with them count under "unknown"."""
    out = {}

    def row(k):
        return out.setdefault(k or "unknown", dict.fromkeys(("added", "open", "done", "parked", "not_useful",
                                                             "dismissed"), 0))
    for i in load(cfg):
        if i.get("source") != "session":
            continue
        r = row(i.get("kind"))
        r["added"] += 1
        r["done" if i.get("done_at") else "open" if is_open(i) else "parked"] += 1
    for d in dismissed(cfg):
        r = row(d.get("kind"))
        r["added"] += 1
        r["not_useful" if d.get("reason") == "not_useful" else "dismissed"] += 1
    return out


# ---------- export ----------
FORMATS = ("md", "csv", "json")
_PARKED_LABEL = {"hold": "on hold", "archived": "archived"}


def state(item):
    """open, doing, hold, archived or done."""
    if item.get("done_at"):
        return "done"
    return item.get("status") if item.get("status") in STATUSES else "doing" if item.get("doing") else "open"


def resume(item):
    return f"cd {item.get('cwd') or '~'} && claude --resume {item['session']}" if item.get("session") else ""


def _cell(v):
    """A CSV cell a spreadsheet won't run as a formula."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def export(items, fmt="md", project=None, status="open"):
    """The to-do list as Markdown (grouped by project), CSV or JSON text. status: open (not done or
    parked) or all. Nothing is sent anywhere; this only formats what is in todos.json."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if status not in ("open", "all"):
        raise ValueError("status must be open or all")
    rows = [i for i in items if (project is None or (i.get("project") or "") == project)
            and (status == "all" or is_open(i))]
    rows.sort(key=lambda i: ((i.get("project") or "").lower(), state(i) == "done", i.get("created") or ""))
    if fmt == "json":
        return json.dumps(rows, indent=1, ensure_ascii=False) + "\n"
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(("id", "text", "project", "kind", "state", "created", "done_at", "detail", "quote",
                    "duplicate_of", "resume"))
        for i in rows:
            w.writerow([_cell(v) for v in (i["id"], i["text"], i.get("project"), i.get("kind"), state(i),
                                           i.get("created"), i.get("done_at"), i.get("detail"),
                                           (i.get("evidence") or {}).get("quote"), i.get("duplicate_of"),
                                           resume(i))])
        return out.getvalue()
    lines = [f"# To-dos ({'open' if status == 'open' else 'all'}, exported {_now()[:10]})", ""]
    current = object()
    for i in rows:
        if (i.get("project") or "") != current:
            current = i.get("project") or ""
            lines += ["", f"## {current or 'No project'}", ""]
        st = state(i)
        tags = [t for t in (i.get("kind"), _PARKED_LABEL.get(st), "doing" if st == "doing" else None) if t]
        done = f" (done {i['done_at'][:10]})" if i.get("done_at") else ""
        lines.append(f"- [{'x' if st == 'done' else ' '}] {i['text']}" + (f" *({', '.join(tags)})*" if tags else "")
                     + done)
        if i.get("detail"):
            lines.append(f"  {i['detail']}")
        quote = (i.get("evidence") or {}).get("quote")
        if quote:
            lines.append(f"  > {quote}")
        if i.get("session"):
            lines.append(f"  `{resume(i)}`")
    if not rows:
        lines.append("Nothing to export.")
    return "\n".join(lines).replace("\n\n\n", "\n\n") + "\n"
