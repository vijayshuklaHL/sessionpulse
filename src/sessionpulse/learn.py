"""The dashboard's "Get better at Claude Code" section.

LIBRARY is a short, hand-checked list of free, official places to learn.
suggest() looks at your own sessions and puts the most relevant items first,
with the reason taken from your data ("9 sessions ran past 40 prompts").
It only reads what the reader already parsed, plus whether CLAUDE.md exists in
your busiest project folders and whether settings.json has hooks of your own.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

DOCS = "https://code.claude.com/docs/en/"

# level: start = new to Claude Code, better = using it daily, advanced = extending it.
LIBRARY = [
    {"id": "quickstart", "level": "start", "kind": "Docs", "title": "Quickstart",
     "url": DOCS + "quickstart", "blurb": "Your first real task, step by step."},
    {"id": "course-cc", "level": "better", "kind": "Course",
     "title": "Claude Code in Action (Anthropic Academy)",
     "url": "https://anthropic.skilljar.com/claude-code-in-action",
     "blurb": "Free course for daily users: long sessions, CLAUDE.md and skills, automating repeat work."},
    {"id": "best-practices", "level": "start", "kind": "Docs", "title": "Best practices for Claude Code",
     "url": DOCS + "best-practices", "blurb": "How to give context, check work and keep sessions on track."},
    {"id": "workflows", "level": "start", "kind": "Docs", "title": "Common workflows",
     "url": DOCS + "common-workflows", "blurb": "Recipes: understand a codebase, fix bugs, refactor, write tests, open PRs."},
    {"id": "eng-blog", "level": "better", "kind": "Article", "title": "Claude Code: best practices for agentic coding",
     "url": "https://www.anthropic.com/engineering/claude-code-best-practices",
     "blurb": "Anthropic's engineers on the habits that work: explore, plan, code, commit."},
    {"id": "memory", "level": "better", "kind": "Docs", "title": "How Claude remembers your project (CLAUDE.md)",
     "url": DOCS + "memory", "blurb": "Write project instructions once instead of repeating them every session."},
    {"id": "context", "level": "better", "kind": "Docs", "title": "Explore the context window",
     "url": DOCS + "context-window", "blurb": "What fills the context, and when to /clear or /compact."},
    {"id": "costs", "level": "better", "kind": "Docs", "title": "Manage costs effectively",
     "url": DOCS + "costs", "blurb": "Keep token use down without slowing yourself down."},
    {"id": "plan-mode", "level": "better", "kind": "Docs", "title": "Permission modes, including plan mode",
     "url": DOCS + "permission-modes", "blurb": "Have Claude plan and agree the approach before it edits anything."},
    {"id": "prompting", "level": "better", "kind": "Docs", "title": "Prompt engineering overview",
     "url": "https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview",
     "blurb": "Be clear and specific, give examples, say what done looks like."},
    {"id": "prompt-tutorial", "level": "better", "kind": "Tutorial", "title": "Interactive prompt engineering tutorial",
     "url": "https://github.com/anthropics/prompt-eng-interactive-tutorial",
     "blurb": "Nine hands-on chapters with exercises, from Anthropic."},
    {"id": "subagents", "level": "advanced", "kind": "Docs", "title": "Create custom subagents",
     "url": DOCS + "sub-agents", "blurb": "Hand research or review to a helper with its own context."},
    {"id": "skills", "level": "advanced", "kind": "Docs", "title": "Extend Claude with skills",
     "url": DOCS + "skills", "blurb": "Package a procedure you repeat so Claude follows it the same way every time."},
    {"id": "course-skills", "level": "advanced", "kind": "Course",
     "title": "Introduction to agent skills (Anthropic Academy)",
     "url": "https://anthropic.skilljar.com/introduction-to-agent-skills",
     "blurb": "Free course on writing and using skills."},
    {"id": "mcp", "level": "advanced", "kind": "Docs", "title": "Connect Claude Code to tools via MCP",
     "url": DOCS + "mcp", "blurb": "Let Claude use your issue tracker, database, browser or chat."},
    {"id": "course-mcp", "level": "advanced", "kind": "Course",
     "title": "Introduction to Model Context Protocol (Anthropic Academy)",
     "url": "https://anthropic.skilljar.com/introduction-to-model-context-protocol",
     "blurb": "Free course: how MCP servers work and how to build one."},
    {"id": "hooks", "level": "advanced", "kind": "Docs", "title": "Automate actions with hooks",
     "url": DOCS + "hooks-guide", "blurb": "Run formatters, checks or notifications automatically at set points."},
    {"id": "worktrees", "level": "advanced", "kind": "Docs", "title": "Run parallel sessions with worktrees",
     "url": DOCS + "worktrees", "blurb": "Work on several tasks at once without them touching each other."},
    {"id": "headless", "level": "advanced", "kind": "Docs", "title": "Run Claude Code programmatically",
     "url": DOCS + "headless", "blurb": "Use Claude Code from scripts, cron and CI."},
    {"id": "whats-new", "level": "current", "kind": "Docs", "title": "What's new (weekly)",
     "url": DOCS + "whats-new", "blurb": "A short weekly note on new features."},
    {"id": "academy", "level": "current", "kind": "Course", "title": "All Anthropic Academy courses",
     "url": "https://anthropic.skilljar.com/", "blurb": "Every free course, including the API and MCP tracks."},
]

WINDOW_DAYS = 30
MIN_SESSIONS = 5       # below this there isn't enough to judge habits
LONG_PROMPTS = 40      # a session past this many prompts is "long"
SHORT_WORDS = 4        # a first prompt this short rarely carries enough context
BUSIEST = 8            # how many projects to check for CLAUDE.md


def _has_memory(cwd):
    p = Path(cwd)
    return any((p / f).is_file() for f in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"))


def _own_hooks(cfg):
    """True/False if settings.json has hooks besides Session Pulse's; None if unreadable."""
    try:
        hooks = json.loads((cfg.claude / "settings.json").read_text()).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        return None
    text = json.dumps(hooks)
    return text.count('"command"') > text.count("-m sessionpulse hook")


def suggest(cfg, sessions, now=None):
    now = now or datetime.now().astimezone()
    cutoff = (now - timedelta(days=WINDOW_DAYS)).isoformat()
    recent = [s for s in sessions if (s.get("end") or "") >= cutoff and s.get("prompts")]
    out = {"window_days": WINDOW_DAYS, "sessions": len(recent), "suggestions": [],
           "strengths": [], "library": LIBRARY}
    if len(recent) < MIN_SESSIONS:
        return out

    tools = {}
    for s in sessions:  # feature use counts over all history: one use is enough to know it
        for k, v in (s.get("tool_names") or {}).items():
            tools[k] = tools.get(k, 0) + v
    agents = sum(s.get("agents") or 0 for s in sessions)

    def add(sid, title, why, links):
        out["suggestions"].append({"id": sid, "title": title, "why": why, "links": links})

    hours = {}
    for s in recent:
        if s.get("cwd"):
            hours[s["cwd"]] = hours.get(s["cwd"], 0) + (s.get("hours") or 0)
    busiest = sorted(hours, key=hours.get, reverse=True)[:BUSIEST]
    missing = [s for s in busiest if Path(s) != Path.home() and Path(s).is_dir() and not _has_memory(s)]
    if missing:
        names = ", ".join(Path(m).name for m in missing[:4]) + (" …" if len(missing) > 4 else "")
        add("memory", "Give your projects a CLAUDE.md",
            f"{len(missing)} of your {len(busiest)} busiest projects have none ({names}). "
            "Run /init in one to start it.", ["memory", "best-practices"])
    else:
        out["strengths"].append("Your busiest projects have a CLAUDE.md")

    long_ = [s for s in recent if (s.get("prompts") or 0) >= LONG_PROMPTS]
    if len(long_) >= 2:
        add("context", "Keep sessions short and focused",
            f"{len(long_)} sessions in the last {WINDOW_DAYS} days ran past {LONG_PROMPTS} prompts. "
            "Long sessions fill the context, which costs tokens and makes earlier details easier to lose. "
            "Use /clear between unrelated tasks.", ["context", "costs", "course-cc"])

    short = [s for s in recent if 0 < len((s.get("first_prompt") or "").split()) <= SHORT_WORDS]
    if len(short) >= 3 and len(short) / len(recent) >= 0.25:
        add("prompting", "Open with more context",
            f"{len(short)} of {len(recent)} recent sessions started with {SHORT_WORDS} words or fewer. "
            "Say what you want, where it is and how you'll judge it done.",
            ["prompting", "prompt-tutorial", "eng-blog"])

    def feature(used, sid, title, why, links, strength):
        if used:
            out["strengths"].append(strength)
        else:
            add(sid, title, why, links)

    feature(tools.get("ExitPlanMode") or tools.get("EnterPlanMode"), "plan-mode", "Try plan mode on bigger changes",
            "None of your sessions used plan mode. Press Shift+Tab until it shows plan mode, and Claude "
            "proposes a plan before it touches files.", ["plan-mode", "workflows"], "You use plan mode")
    feature(agents or tools.get("Agent") or tools.get("Task"), "subagents", "Hand side-work to subagents",
            "No session used a subagent yet. They do research or reviews in their own context, "
            "so your main session stays clean.", ["subagents"], "You use subagents")
    feature(tools.get("Skill"), "skills", "Turn repeated instructions into a skill",
            "No session used a skill yet.", ["skills", "course-skills"], "You use skills")
    feature(any(k.startswith("mcp__") for k in tools), "mcp", "Connect the tools you work in",
            "No session used an MCP server yet.", ["mcp", "course-mcp"], "You connect tools through MCP")
    own = _own_hooks(cfg)
    if own is not None:
        feature(own, "hooks", "Automate the routine with hooks",
                "Your settings have no hooks of your own yet.", ["hooks"], "You automate with hooks")
    return out
