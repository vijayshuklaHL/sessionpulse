"""Build a fictional Session Pulse world for the showcase video (tools/showcase/run.sh). Nothing here is real data.

Layout (all under DEMO):
  home/work/<project>/           fake project folders (some with CLAUDE.md)
  home/.claude/projects/...       fake transcripts
  home/.claude/sessions/*.json    fake live sessions (pids of `sleep` processes)
  data/sessionpulse/              events, to-dos, token
  config/sessionpulse/config.toml port 8811, follow-ups off
"""
import json
import random
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DEMO = Path(sys.argv[1]).resolve()
random.seed(7)
FAKE_HOME = "/home/maya"
NOW = datetime.now().astimezone().replace(second=0, microsecond=0)

PROJECTS = {
    "work/orders-api": ["Add rate limiting to the /v1/orders endpoint",
                        "Why does the checkout integration test flake on CI?",
                        "Write a migration to add refund_reason to payments",
                        "Review the PR that moves order events to the queue"],
    "work/mobile-app": ["Fix the iOS crash when the cart is empty",
                        "Add offline mode for the product list",
                        "Profile the slow startup on Android"],
    "work/infra-terraform": ["Plan the move of staging to the new VPC",
                             "Why did the nightly backup job fail?",
                             "Tighten the IAM policy for the deploy role"],
    "work/billing-service": ["Reconcile the invoice totals with Stripe exports",
                             "Add tax handling for EU customers"],
    "work/docs-site": ["Rewrite the getting-started guide",
                       "Generate API reference pages from the OpenAPI spec"],
    "work/data-pipeline": ["Backfill the events table for September",
                           "Speed up the daily revenue job"],
}
WITH_MEMORY = {"work/orders-api", "work/mobile-app", "work/billing-service"}
FOLLOWUPS = [
    # (project, kind, text, detail)
    ("work/orders-api", "decision", "Choose the rate limit: 100 or 300 requests per minute per key",
     "Load test showed both are safe; 300 matches what the two largest customers send at peak."),
    ("work/orders-api", "external", "Ask the payments team to approve the new refund_reason column",
     "Migration is ready on the branch; their reporting job reads the payments table."),
    ("work/orders-api", "checklist", "Verify the order-events queue drains after the deploy",
     "Watch the consumer lag dashboard for 30 minutes after release."),
    ("work/orders-api", "task", "Remove the retry wrapper once the flaky test is fixed",
     "Left in place to unblock CI; tracked in the PR description."),
    ("work/mobile-app", "waiting", "Crash report from the beta testers on iOS 19.1",
     "TestFlight build 214 went out yesterday with the empty-cart fix."),
    ("work/mobile-app", "decision", "Decide whether offline mode caches product images",
     "Caching images adds ~40 MB on device; text-only cache is 2 MB."),
    ("work/mobile-app", "external", "Renew the Apple developer certificate before 2 November",
     "Push notifications stop working when it expires."),
    ("work/infra-terraform", "checklist", "Audit the security groups left open on staging",
     "Three groups allow 0.0.0.0/0 on port 22; list is in the plan output."),
    ("work/infra-terraform", "external", "Book the maintenance window for the VPC move with support",
     "Needs 45 minutes; customers get 72 hours notice."),
    ("work/infra-terraform", "decision", "Pick the backup retention: 14 or 30 days",
     "30 days doubles storage cost; compliance asks for at least 14."),
    ("work/infra-terraform", "checklist", "Confirm last night's backup restored cleanly",
     "Run the restore test against the scratch database."),
    ("work/infra-terraform", "waiting", "Cloud provider's answer on the quota increase",
     "Ticket opened Monday; the VPC move needs 4 more NAT gateways."),
    ("work/billing-service", "external", "Send the EU tax rules summary to the accountant",
     "Draft is in docs/tax-eu.md; needs sign-off before launch."),
    ("work/billing-service", "decision", "Decide how to round tax on multi-currency invoices",
     "Per-line rounding differs from the Stripe export by up to 0.02."),
    ("work/billing-service", "task", "Add a test for invoices with zero-rated items",
     "The reconciliation found one mismatch caused by this case."),
    ("work/docs-site", "checklist", "Check every code sample in the getting-started guide runs",
     "Two samples still use the old client constructor."),
    ("work/docs-site", "task", "Publish the generated API reference to the preview site",
     "Build passes; preview deploy was skipped."),
    ("work/data-pipeline", "waiting", "Finance to confirm September revenue after the backfill",
     "Totals changed by 0.4% after late events were included."),
    ("work/data-pipeline", "external", "Tell the analytics channel the daily job now finishes by 6:00",
     "Was finishing around 8:30; dashboards can refresh earlier."),
    ("work/data-pipeline", "decision", "Decide whether to keep the old revenue job running in parallel for a week",
     "Running both costs about two hours of warehouse time per day."),
]
DONE = [
    ("work/orders-api", "task", "Fix the flaky checkout integration test", True),
    ("work/mobile-app", "external", "Ship TestFlight build 214 to beta testers", True),
    ("work/infra-terraform", "task", "Tighten the IAM policy for the deploy role", False),
    ("work/billing-service", "checklist", "Reconcile September invoices with the Stripe export", True),
    ("work/docs-site", "decision", "Choose the docs theme", False),
]
MANUAL = [("Prepare slides for Thursday's demo", None), ("1:1 notes for Friday", None)]
REPLIES = ["Done. The change is on the branch and the tests pass.",
           "I found the cause: the test depends on wall-clock time. I made it use a fixed clock.",
           "Here's the plan, step by step. Nothing has been changed yet.",
           "Still open: you need to decide the limit, and the payments team has to approve the column."]
TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Agent", "Skill", "mcp__github__create_pull_request",
         "mcp__linear__update_issue"]


def iso(t):
    return t.astimezone().isoformat()


def write_session(proj, start, minutes, prompts, model, sid=None, live=False):
    sid = sid or str(uuid.uuid4())
    cwd = f"{FAKE_HOME}/{proj}"  # a fictional path: nothing in the video points at this machine
    pdir = DEMO / "home/.claude/projects" / ("-" + cwd.strip("/").replace("/", "-"))
    pdir.mkdir(parents=True, exist_ok=True)
    rows, t, n = [], start, 0
    asks = PROJECTS[proj]
    step = timedelta(minutes=max(1, minutes / max(prompts, 1)))
    for i in range(prompts):
        text = asks[i % len(asks)] if i == 0 or random.random() < .3 else random.choice(
            ["yes, go ahead", "run the tests again", "what changed?", "keep it simple",
             "check the logs from last night", "open a PR when it's green"])
        if i == 0:
            text = random.choice(asks)
        rows.append({"type": "user", "timestamp": iso(t), "cwd": cwd, "sessionId": sid, "version": "2.1.287",
                     "message": {"role": "user", "content": text}})
        for k in range(random.randint(1, 3)):
            t += timedelta(seconds=random.randint(20, 90))
            n += 1
            content = [{"type": "tool_use", "name": random.choice(TOOLS)}] if k else \
                [{"type": "text", "text": random.choice(REPLIES)}]
            rows.append({"type": "assistant", "timestamp": iso(t), "cwd": cwd, "sessionId": sid, "version": "2.1.287",
                         "message": {"id": f"msg_{sid[:8]}_{n}", "role": "assistant", "model": model,
                                     "content": content,
                                     "usage": {"input_tokens": random.randint(3, 30),
                                               "output_tokens": random.randint(300, 2600),
                                               "cache_creation_input_tokens": random.randint(1500, 18000),
                                               "cache_read_input_tokens": random.randint(30000, 160000)}}})
        t += step
        if live and t > NOW:
            break
    with open(pdir / f"{sid}.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return {"id": sid, "project": proj, "cwd": cwd, "end": rows[-1]["timestamp"], "start": rows[0]["timestamp"]}


def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    for p in PROJECTS:
        d = DEMO / "home" / p
        d.mkdir(parents=True)
        if p in WITH_MEMORY:
            (d / "CLAUDE.md").write_text("# Project notes\n")
    (DEMO / "home/.claude/sessions").mkdir(parents=True)
    (DEMO / "home/.claude/settings.json").write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [
        {"type": "command", "command": "npx prettier --write"}]}]}}))
    data = DEMO / "data/sessionpulse"
    data.mkdir(parents=True)
    cfgd = DEMO / "config/sessionpulse"
    cfgd.mkdir(parents=True)
    (cfgd / "config.toml").write_text("port = 8811\nrefresh_seconds = 3\nfollowups = false\n")

    sessions, events = [], []
    projects = list(PROJECTS)
    weights = [5, 3, 4, 2, 1, 2]
    for day in range(29, 0, -1):
        date = NOW - timedelta(days=day)
        if date.weekday() >= 5 and random.random() < .7:
            continue
        for _ in range(random.choice([1, 1, 2, 2, 3])):
            proj = random.choices(projects, weights)[0]
            start = date.replace(hour=random.randint(9, 18), minute=random.choice([0, 10, 25, 40, 55]))
            model = "claude-sonnet-5-5" if day > 18 else random.choice(
                ["claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"])
            long_ = random.random() < .08
            s = write_session(proj, start, random.randint(25, 150) * (2 if long_ else 1),
                              random.randint(45, 60) if long_ else random.randint(3, 22), model)
            sessions.append(s)
            events.append({"ts": s["start"], "event": "SessionStart", "session": s["id"], "cwd": s["cwd"],
                           "reason": "startup"})
            events.append({"ts": s["end"], "event": "SessionEnd", "session": s["id"], "cwd": s["cwd"],
                           "reason": random.choice(["prompt_input_exit", "prompt_input_exit", "clear", "other"])})
    # earlier today, finished
    for proj, hrs in (("work/billing-service", 5), ("work/docs-site", 3)):
        s = write_session(proj, NOW - timedelta(hours=hrs), 50, 8, "claude-opus-5-5")
        sessions.append(s)
        events += [{"ts": s["start"], "event": "SessionStart", "session": s["id"], "cwd": s["cwd"], "reason": "startup"},
                   {"ts": s["end"], "event": "SessionEnd", "session": s["id"], "cwd": s["cwd"],
                    "reason": "prompt_input_exit"}]

    # live sessions: real `sleep` processes so the pid check passes
    live = []
    for proj, mins, status in (("work/orders-api", 95, "busy"), ("work/infra-terraform", 40, "idle"),
                               ("work/mobile-app", 18, "idle")):
        sid = str(uuid.uuid4())
        s = write_session(proj, NOW - timedelta(minutes=mins), mins, max(3, mins // 8), "claude-opus-5-5",
                          sid=sid, live=True)
        p = subprocess.Popen(["sleep", "7200"], start_new_session=True)
        started = int((NOW - timedelta(minutes=mins)).timestamp() * 1000)
        (DEMO / f"home/.claude/sessions/{p.pid}.json").write_text(json.dumps({
            "pid": p.pid, "sessionId": sid, "status": status, "kind": "interactive", "entrypoint": "cli",
            "startedAt": started, "statusUpdatedAt": int(NOW.timestamp() * 1000) - random.randint(5, 200) * 1000}))
        events.append({"ts": s["start"], "event": "SessionStart", "session": sid, "cwd": s["cwd"], "reason": "startup"})
        live.append({"pid": p.pid, "sid": sid, "project": proj})
    events.sort(key=lambda e: e["ts"])
    with open(data / "events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    by_proj = {}
    for s in sessions:
        by_proj.setdefault(s["project"], []).append(s)
    todos, hold = [], []
    for i, (proj, kind, text, detail) in enumerate(FOLLOWUPS):
        s = random.choice(by_proj[proj][-4:])
        item = {"id": f"d{i:03x}", "text": text, "project": proj, "created": s["end"], "done_at": None,
                "source": "session", "kind": kind, "detail": detail, "session": s["id"], "cwd": s["cwd"],
                "session_at": s["end"]}
        # three items arrive on camera (the recorder adds them), the rest are already there
        (hold if proj == "work/orders-api" and kind != "task" else todos).append(item)
    todos[3]["doing"] = True
    for j, (proj, kind, text, auto) in enumerate(DONE):
        s = by_proj[proj][-1]
        done = iso(NOW - timedelta(hours=random.randint(2, 70)))
        item = {"id": f"x{j:03x}", "text": text, "project": proj, "created": s["start"], "done_at": done,
                "source": "session", "kind": kind, "detail": None, "session": s["id"], "cwd": s["cwd"],
                "session_at": s["start"]}
        if auto:
            item["closed_by"] = s["id"]
        todos.append(item)
    for k, (text, proj) in enumerate(MANUAL):
        todos.append({"id": f"m{k:03x}", "text": text, "project": proj,
                      "created": iso(NOW - timedelta(hours=20 + k)), "done_at": None})
    (data / "todos.json").write_text(json.dumps(todos, indent=1))
    (DEMO / "arrivals.json").write_text(json.dumps(hold, indent=1))
    (DEMO / "live.json").write_text(json.dumps(live, indent=1))
    print(f"{len(sessions) + len(live)} sessions, {len(todos)} to-dos, {len(hold)} arriving on camera, "
          f"live pids {[l['pid'] for l in live]}")


main()
