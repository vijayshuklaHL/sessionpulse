# Contributing

Thanks for wanting to make Session Pulse better. Bug reports, ideas and pull requests are all welcome.

## Ground rules

- **No new runtime dependencies.** Session Pulse uses only the Python standard library (plus PyGObject for the optional panel icon). That keeps installs trivial and the attack surface small.
- **Local and private by default.** Nothing may send data off the machine unless the user turns it on, the way `followups = true` does.
- **Read Claude Code's files, never write them.** The one exception is `install`/`uninstall`, which edit only our own hooks after a backup.
- **Claude Code's file formats are undocumented.** Parse defensively; count anything surprising in `health` rather than crash.

## Getting started

```sh
git clone https://github.com/<you>/sessionpulse && cd sessionpulse
python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m sessionpulse serve      # in another terminal:
PYTHONPATH=src python3 -m sessionpulse open
```

The dashboard is one file, `src/sessionpulse/static/dashboard.html`. Restart `serve` after editing it.

To try things without your own sessions, `tools/showcase/make_demo.py` builds a made-up `~/.claude`; see `tools/showcase/run.sh` for how to point a server at it.

## Measuring follow-ups

Changes to the follow-up prompt, schema or merging should be measured, not guessed. `tools/eval_followups.py` runs the real `followups.process()` on the made-up cases in `tests/fixtures/followups/` (each one a short, readable spec of sessions and what should come out) and scores the list as it ends: open items found, finished items wrongly left open, duplicates, closures, leaked secrets and model usage. It calls `claude -p`, so it counts toward your usage (a full run with `--repeat 3` costs a few cents on haiku), and it never touches your own data.

```sh
python3 tools/eval_followups.py --repeat 3 --out eval/mine.json
python3 tools/eval_followups.py --compare eval/0.1.3.json eval/mine.json
```

Include the comparison in pull requests that change follow-ups. New cases are welcome: made-up content only.

## Pull requests

- Keep each PR to one change, and add or update a test in `tests/` when behaviour changes.
- Run `python3 -m unittest discover -s tests` before pushing; CI runs it on Python 3.11–3.13.
- For UI changes, include a screenshot in light and dark mode, and check it at phone width.
- Note user-visible changes in `CHANGELOG.md`.

## Reporting bugs

Please include your OS, Python version, Claude Code version (`claude --version`) and the output of `sessionpulse doctor`. Don't paste transcripts or anything from `~/.claude` unless you've checked it for private details.
