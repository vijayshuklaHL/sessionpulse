#!/usr/bin/env python3
"""Measure how well follow-ups work, on made-up transcripts.

Each case in tests/fixtures/followups/ is a short spec: one or more sessions
(turns of user and Claude text, plus broken lines, tool results, subagent turns
or filler), to-dos already on the list, and what should come out. This script
writes the sessions as Claude Code transcripts in a throwaway directory, runs
the real followups.process() on them with `claude -p`, and scores the result:

  recall        expected open items that were found
  hard          the same, for items the current design is known to miss
  wrong_open    items added that were finished, rejected or already listed
                (items in a case's `ok` list are fine either way and not counted)
  folded        expected items found only in another open item's detail, as a step that waits on it
                (counted as found)
  duplicates    extra open items for work already found (in any project)
  flagged       of those, copies shown as "maybe the same as" the other (one click to merge)
  closures      seeded to-dos correctly closed, and wrongly closed
  leaks         secrets copied into an item
  quoted        items whose quote was found in the transcript (only those keep one)
  unverified    quotes the model gave that aren't in the transcript (so were dropped)
  tokens, cost  what the model calls used, from the `claude -p` JSON

It calls the real model (counts toward your Claude usage) and never touches
your own data. Run by hand:

  python3 tools/eval_followups.py --out eval/0.1.3.json
  python3 tools/eval_followups.py --only 07,08 --repeat 3
  python3 tools/eval_followups.py --compare eval/0.1.3.json eval/0.2.0.json
  python3 tools/eval_followups.py --rescore eval/0.1.3.json   # after changing a case's rules
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest.mock
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sessionpulse import __version__, config, followups, reader, todos  # noqa: E402

CASES = ROOT / "tests" / "fixtures" / "followups"
START = datetime(2026, 10, 20, 9, 0).astimezone()
_real_run = subprocess.run
_real_evidence = followups.evidence
_local = threading.local()


# ---------- building transcripts ----------
FILLER_TOPICS = ["the timestamp parser", "the JSON field mapping", "the batch writer", "the retry logic",
                 "the schema checks", "the gzip reader", "the field renames", "the test data"]


def filler(n):
    """A long, dull exchange about the pipeline: about 2,500 characters of reply per turn."""
    topic = FILLER_TOPICS[n % len(FILLER_TOPICS)]
    reply = (f"Step {n + 1}: working on {topic}. " +
             f"I read the module, ran its tests and adjusted {topic} so it handles the sample files in "
             f"fixtures/batch-{n:02d}. Each sample has a few hundred lines with mixed timestamp formats, missing "
             f"fields and very long messages, so I checked them one by one and kept the behaviour the same for "
             f"well-formed input. " * 9)
    return [["user", f"Next: {topic} (part {n + 1})."], ["claude", reply[:2500]]]


def _entry(sid, cwd, t, typ, content, **kw):
    return {"type": typ, "timestamp": t.isoformat(), "uuid": str(uuid.uuid4()), "cwd": cwd, "sessionId": sid,
            "version": "2.1.287", "message": {"role": "user" if typ == "user" else "assistant",
                                              "content": content}, **kw}


def build(name, spec, claude_dir):
    """Write the case's sessions as transcripts; return [(session dict, path)]."""
    out = []
    for n, s in enumerate(spec["sessions"]):
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sessionpulse-eval/{name}/{n}"))
        cwd = f"/home/demo/{s['project']}"
        path = claude_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        t = START + timedelta(days=n)
        turns = []
        for turn in s["turns"]:
            turns += filler_turns(turn["filler"]) if isinstance(turn, dict) and "filler" in turn else [turn]
        lines = []
        for turn in turns:
            t += timedelta(minutes=2)
            if isinstance(turn, list):
                role, text = turn
                lines.append(json.dumps(_entry(sid, cwd, t, "user" if role == "user" else "assistant",
                                               text if role == "user" else [{"type": "text", "text": text}])))
            elif "raw" in turn:
                lines.append(turn["raw"])
            elif "tool_result" in turn:
                lines.append(json.dumps(_entry(sid, cwd, t, "user", [
                    {"type": "tool_result", "tool_use_id": "toolu_eval", "content": turn["tool_result"]}])))
            elif "sidechain" in turn:
                lines.append(json.dumps(_entry(sid, cwd, t, "assistant", [
                    {"type": "text", "text": turn["sidechain"][1]}], isSidechain=True)))
        path.write_text("\n".join(lines) + "\n")
        out.append(({"id": sid, "project": s["project"], "cwd": cwd, "end": t.isoformat(timespec="seconds")},
                    path))
    return out


def filler_turns(count):
    return [x for n in range(count) for x in filler(n)]


# ---------- scoring ----------
def matches(text, rule, detail=""):
    """Every group of the rule has one of its words in the text or detail, and none of the
    `unless` words is in the text."""
    text, full = text.lower(), f"{text} {detail or ''}".lower()
    return (all(any(w in full for w in group) for group in rule["match"])
            and not any(w in text for w in rule.get("unless", ())))


def score(spec, added, final):
    """Score one run on the list as it ends. `added` are the new items, `final` the whole list."""
    exp = spec["expect"]
    hits = {n: [] for n in range(len(exp["open"]))}
    wrong, extra, fine, later = [], [], [], []
    for item in added:
        if item.get("done_at"):  # added, then closed by a later session: judge the list as it ends
            later.append(item["text"])
            continue
        found = [n for n, rule in enumerate(exp["open"]) if matches(item["text"], rule, item.get("detail"))]
        if found:  # credit an expected item not found yet, so a near match isn't counted as a duplicate
            hits[next((n for n in found if not hits[n]), found[0])].append(item)
        elif any(matches(item["text"], rule, item.get("detail")) for rule in exp.get("ok", ())):
            fine.append(item["text"])
        elif any(matches(item["text"], rule) for rule in exp.get("not_open", ())):
            wrong.append(item["text"])
        else:
            extra.append(item["text"])
    folded = 0
    for n, rule in enumerate(exp["open"]):  # a step noted on the open item it waits on is not lost
        if not hits[n] and any(todos.is_open(i) and matches(i.get("detail") or "", rule) for i in final):
            hits[n].append({})
            folded += 1
    easy = [n for n, rule in enumerate(exp["open"]) if not rule.get("hard")]
    hard = [n for n, rule in enumerate(exp["open"]) if rule.get("hard")]
    by_id = {i["id"]: i for i in final}
    return {
        "expected": len(easy), "found": sum(1 for n in easy if hits[n]),
        "hard_expected": len(hard), "hard_found": sum(1 for n in hard if hits[n]),
        "missed": [exp["open"][n]["label"] for n in range(len(exp["open"])) if not hits[n]],
        "wrong_open": wrong,
        "folded": folded,
        "duplicates": sum(max(0, len(h) - 1) for h in hits.values()),
        "flagged": sum(1 for h in hits.values() for i in h
                       if i.get("possible_duplicate_of") and i["possible_duplicate_of"] in {x.get("id") for x in h}),
        "extra": extra,
        "ok": fine,
        "closed_later": later,
        "closed_expected": len(exp.get("closed", ())),
        "closed_ok": sum(1 for i in exp.get("closed", ()) if by_id.get(i, {}).get("done_at")),
        "closed_wrongly": [i for i in exp.get("kept_open", ()) if by_id.get(i, {}).get("done_at")],
        "leaks": [s for s in exp.get("never_contains", ())
                  for i in added if s.lower() in f"{i['text']} {i.get('detail') or ''}".lower()],
        "quoted": sum(1 for i in added if i.get("evidence")),
    }


# ---------- running ----------
def _counting_run(cmd, *a, **kw):
    """subprocess.run, recording what each `claude -p` call used."""
    r = _real_run(cmd, *a, **kw)
    use = getattr(_local, "usage", None)
    if use is not None:
        try:
            out = json.loads(r.stdout)
            u = out.get("usage") or {}
            use["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                use[k] += u.get(k) or 0
            use["cost_usd"] += out.get("total_cost_usd") or 0
            use["model_ms"] += out.get("duration_ms") or 0
        except (ValueError, AttributeError):
            use["calls"] += 1
    return r


def _checking_evidence(reply, turns):
    """followups.evidence, remembering the quotes it couldn't find."""
    ev = _real_evidence(reply, turns)
    if ev is None and getattr(_local, "unverified", None) is not None:
        _local.unverified.append(str(reply.get("quote"))[:200])
    return ev


def run_case(name, spec, model, claude_command=""):
    _local.usage = dict.fromkeys(("calls", "input_tokens", "output_tokens", "cache_creation_input_tokens",
                                  "cache_read_input_tokens", "cost_usd", "model_ms"), 0)
    _local.unverified = []
    began = time.monotonic()
    errors = []
    with tempfile.TemporaryDirectory(prefix="sp-eval-") as tmp:
        root = Path(tmp)
        cfg = config.Config(claude_dir=str(root / "claude"), data_dir=str(root / "data"), port=0,
                            followups_model=model, claude_command=claude_command)
        cfg.data.mkdir(parents=True)
        seed = [{"created": START.isoformat(timespec="seconds"), "done_at": None, "source": "session", **i}
                for i in spec.get("seed", ())]
        if seed:
            todos._save(cfg, seed)
        for a, b in spec.get("related", ()):  # projects linked by an earlier merge
            todos._link(cfg, "related", a, b)
        for sess, path in build(name, spec, cfg.claude):
            try:
                followups.process(cfg, sess, path)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc)[:300])
        final = todos.load(cfg)
    seeded = {i["id"] for i in seed}
    added = [i for i in final if i["id"] not in seeded]
    result = score(spec, added, final)
    result.update(errors=errors, seconds=round(time.monotonic() - began, 1), usage=_local.usage,
                  unverified=_local.unverified,
                  final_open=[{"id": i["id"], "detail": i.get("detail")} for i in final if todos.is_open(i)],
                  items=[{k: i.get(k) for k in ("id", "text", "kind", "detail", "project", "evidence", "done_at",
                                                "possible_duplicate_of") if i.get(k)}
                         for i in added])
    return result


def load_cases(only=None):
    cases = {}
    for p in sorted(CASES.glob("*.json")):
        if only and not any(p.stem.startswith(o) for o in only):
            continue
        cases[p.stem] = json.loads(p.read_text())
    return cases


def summarise(runs):
    """Totals over every run of every case."""
    flat = [r for rs in runs.values() for r in rs]
    tot = lambda k: sum(r[k] for r in flat)  # noqa: E731
    count = lambda k: sum(len(r[k]) for r in flat)  # noqa: E731
    use = lambda k: sum(r["usage"][k] for r in flat)  # noqa: E731
    sessions = sum(r.get("sessions", 1) for r in flat)
    return {
        "runs": len(flat), "sessions": sessions,
        "recall": round(tot("found") / tot("expected"), 3) if tot("expected") else None,
        "hard_recall": round(tot("hard_found") / tot("hard_expected"), 3) if tot("hard_expected") else None,
        "wrong_open": count("wrong_open"),
        "wrong_open_rate": round(count("wrong_open") / max(1, sum(len(r["items"]) for r in flat)), 3),
        "folded": tot("folded"),
        "duplicates_per_session": round(tot("duplicates") / max(1, sessions), 3),
        "duplicates_flagged": f"{sum(r.get('flagged', 0) for r in flat)}/{tot('duplicates')}",
        "extra": count("extra"),
        "closed": f"{tot('closed_ok')}/{tot('closed_expected')}",
        "closed_wrongly": count("closed_wrongly"),
        "leaks": count("leaks"),
        "quoted": f"{tot('quoted')}/{sum(len(r['items']) for r in flat)}",
        "unverified_quotes": sum(len(r.get("unverified", ())) for r in flat),
        "errors": count("errors"),
        "calls": use("calls"),
        "input_tokens": use("input_tokens") + use("cache_creation_input_tokens") + use("cache_read_input_tokens"),
        "output_tokens": use("output_tokens"),
        "cost_usd": round(use("cost_usd"), 4),
        "model_seconds": round(use("model_ms") / 1000, 1),
    }


def show_case(name, r):
    mark = "ok " if r["found"] == r["expected"] and not r["wrong_open"] and r["duplicates"] <= r.get("flagged", 0) \
        else "!! "
    print(f"{mark}{name}: found {r['found']}/{r['expected']}"
          + (f", hard {r['hard_found']}/{r['hard_expected']}" if r["hard_expected"] else "")
          + (f", closed {r['closed_ok']}/{r['closed_expected']}" if r["closed_expected"] else "")
          + f", {len(r['items'])} added, {r['seconds']}s")
    for i in r["items"]:
        print(f"     + [{i.get('kind')}] {i['text']}" + (f"   ({i['project']})" if i.get("project") else "")
              + ("   (flagged as a possible copy)" if i.get("possible_duplicate_of") else ""))
    for k, label in (("missed", "missed"), ("wrong_open", "wrongly open"), ("extra", "not expected"),
                     ("closed_later", "added, closed later"), ("closed_wrongly", "closed wrongly"), ("leaks", "LEAKED"), ("unverified", "quote not in transcript"),
                     ("errors", "error")):
        for v in r[k]:
            print(f"     - {label}: {v}")


def rescore(path):
    """Score saved answers again with the cases' current rules. No model calls."""
    data, cases = json.loads(Path(path).read_text()), load_cases()
    kept = ("closed_ok", "closed_expected", "closed_wrongly", "errors", "seconds", "usage", "unverified", "final_open",
            "items", "sessions")
    for name, runs in data["cases"].items():
        for r in runs:
            added = [{"id": str(n), **i} for n, i in enumerate(r["items"])]
            keep = {k: r.get(k, []) for k in kept}
            r.clear()
            final = added + [i for i in r.get("final_open", ()) if i["id"] not in {x.get("id") for x in added}]
            r.update(score(cases[name], added, final), **keep)
    data["summary"] = summarise(data["cases"])
    Path(path).write_text(json.dumps(data, indent=1) + "\n")
    return data["summary"]


def compare(a_path, b_path):
    a, b = (json.loads(Path(p).read_text()) for p in (a_path, b_path))
    print(f"{'':24}{a['label']:>16}{b['label']:>16}")
    for k, va in a["summary"].items():
        vb = b["summary"].get(k)
        print(f"{k:24}{str(va):>16}{str(vb):>16}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--model", default="haiku", help="model for the follow-up calls (default: haiku)")
    p.add_argument("--only", help="comma-separated case prefixes, e.g. 01,07")
    p.add_argument("--repeat", type=int, default=1, help="run each case this many times (the model varies)")
    p.add_argument("--parallel", type=int, default=3, help="cases run at the same time")
    p.add_argument("--claude-command", default="", help="path to `claude`, if it isn't on PATH")
    p.add_argument("--label", help="name for this run in the results (default: sessionpulse version)")
    p.add_argument("--out", help="write the results as JSON here")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="compare two result files and exit")
    p.add_argument("--rescore", metavar="FILE", help="score a result file again with the current rules and exit")
    args = p.parse_args(argv)
    if args.rescore:
        for k, v in rescore(args.rescore).items():
            print(f"  {k:24}{v}")
        return 0
    if args.compare:
        compare(*args.compare)
        return 0
    cases = load_cases(args.only.split(",") if args.only else None)
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 1
    cfg = config.Config(claude_command=args.claude_command)
    if not followups.claude_bin(cfg):
        print("the `claude` command was not found; pass --claude-command", file=sys.stderr)
        return 1
    reader._cache.clear()
    jobs = [(name, spec) for name, spec in cases.items() for _ in range(args.repeat)]
    print(f"{len(jobs)} runs of {len(cases)} cases with {args.model}; this calls the real model.", flush=True)
    runs = {name: [] for name in cases}

    def one(job):
        name, spec = job
        r = run_case(name, spec, args.model, args.claude_command)
        r["sessions"] = len(spec["sessions"])
        return name, r

    with unittest.mock.patch.object(followups.subprocess, "run", _counting_run), \
            unittest.mock.patch.object(followups, "evidence", _checking_evidence), \
            ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        for name, r in pool.map(one, jobs):
            runs[name].append(r)
            show_case(name, r)
    summary = summarise(runs)
    print("\nSummary")
    for k, v in summary.items():
        print(f"  {k:24}{v}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        git = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        out.write_text(json.dumps({"label": args.label or __version__, "version": __version__, "git": git,
                                   "model": args.model, "repeat": args.repeat,
                                   "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                   "summary": summary, "cases": runs}, indent=1) + "\n")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
