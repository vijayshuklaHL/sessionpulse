# Changelog

## 0.1.3 (2026-10-29)

- Board: **On hold** and **Archived** columns. Parked items are neither open nor done; `todo list` and the open counts leave them out, and follow-ups never send archived items to Claude. Finishing a held item un-parks it.
- Follow-ups no longer pile up reworded copies of the same to-do. Claude now sees the project's newest 150 open items (it used to get the oldest 40, so busy projects got their recent items added again), and a new item is skipped when it shares most of its words with an open, recently done or dismissed one in the same project.
- `sessionpulse dedupe [-p PROJECT] [--apply]`: asks Claude which open to-dos in each project are the same work reworded and lists them; `--apply` archives the copies, keeping the newest (or the one in progress), each marked `duplicate_of` the one kept.
- `sessionpulse daemon`: the dashboard server and follow-ups without a panel icon, writing `status.json` (open/working sessions, today's totals, open to-dos; no token, mode 0600) every 5 seconds for panel widgets such as the coming GNOME extension. `install --daemon` starts it at login; `doctor` reports whether status.json is fresh. Format: docs/status-json.md.
- GNOME Shell extension (45 to 48) in `gnome-extension/`: a top-bar indicator and menu that read status.json. No network and no subprocesses. Build it with `gnome-extension/pack.sh`.

## 0.1.2 (2026-10-14)

- The dashboard now works through an SSH tunnel on any local port (e.g. `-L 18765:127.0.0.1:8765`); before, anything but the same port got "wrong host". It still only answers to 127.0.0.1/localhost, and writes now need an Origin equal to the address the request went to, so other local pages can't post to it.

## 0.1.1 (2026-10-14)

- Servers without a desktop: `sessionpulse open` now prints an SSH tunnel command and the dashboard link instead of silently doing nothing, `serve` prints the same, and `doctor` explains that the panel icon and autostart don't apply. README: "On a server" setup with a systemd user service.

## 0.1.0 (2026-10-14)

- Artwork from the illustration kit: README hero, illustrated sign-in page, new panel icons (prompt + pulse line in the state colour), dashboard mark that follows the live state, favicon, and a quiet empty state.
- Board tab: kanban of all to-dos by category or by status, with drag and drop, project filter and search; "Open to-do board" in the panel menu.
- Follow-ups: open items picked out of quiet sessions by Claude and added to the to-do list, closed again when a later session finishes them (`followups = true`, `sessionpulse followups`).
- To-do list with a Completed section (dashboard and `sessionpulse todo`).
- "Get better at Claude Code": suggestions based on your own usage, plus a list of free official learning resources.
- First version: dashboard (live sessions, token usage, active time, projects, starts and exits, history), panel icon, `install`/`uninstall` for the hooks and autostart, `doctor`, and warnings when data formats look different.
