# todos.json and friends

Your to-do list lives in `$XDG_DATA_HOME/sessionpulse/` (normally `~/.local/share/sessionpulse/`). Everything is plain JSON, written atomically (temp file + rename) with mode 0600. Session Pulse never sends these files anywhere; `sessionpulse todo export` and the board's **Export** menu only format them.

| File | What it holds |
|---|---|
| `todos.json` | The list: every open, done, on-hold and archived item |
| `todos-dismissed.json` | Follow-ups you dismissed, so they are not picked up again (the newest 2,000) |
| `todos-links.json` | Pairs you marked "not the same", and projects linked by a merge |
| `followups.json` | Per session: when it was read, what it added and closed, what it used, and failures |
| `followups-usage.json` | Model use per day (calls, tokens, cost), for the last 90 days |

Fields may be missing on older items; readers should treat a missing field as empty.

## todos.json

A list of items:

```json
{
  "id": "3f9a1c2e",
  "text": "Get legal's answer on the Inter and Lora font licenses",
  "project": "work/photo-archive",
  "created": "2026-10-30T10:12:00+01:00",
  "done_at": null,
  "source": "session",
  "kind": "waiting",
  "detail": "Legal reviews every new license. Then: Decide on the fonts",
  "session": "0ce4b6e5-e21c-5c11-b4f1-e8bf41c7eaad",
  "cwd": "/home/you/work/photo-archive",
  "session_at": "2026-10-30T09:58:00+01:00",
  "evidence": {"quote": "legal has to approve every new font license", "uuid": "…", "at": "…", "role": "user"}
}
```

- **Your own items** have `id`, `text`, `project`, `created` and `done_at` only.
- **`source: "session"`**: a follow-up picked out of a session. `kind` is one of `external`, `checklist`, `decision`, `waiting` or `task`. `session`, `cwd` and `session_at` say where it came from, so `cd <cwd> && claude --resume <session>` reopens it.
- **`evidence`**: a short quote from the session that shows the item is open, checked word for word against the transcript, and the turn it came from (`uuid`, `at`, `role`). Secrets are removed. Items from before 0.2.0 have none.
- **`done_at`**: when it was ticked, or `null`. When a later session closed it, `closed_by` is that session's id and `closed_evidence` the quote that shows it was done. Reopening it records the session in `not_closed_by`, so that session never closes it again.
- **`status`**: `hold` or `archived` for parked items; missing for active ones. `doing: true` marks an item in progress.
- **`duplicate_of`**: on an archived copy, the id of the item that was kept.
- **`possible_duplicate_of`**: on a follow-up that looks like an open item in another project; the board offers **Merge** and **Not the same**.

## todos-dismissed.json

```json
[{"text": "Look into caching the thumbnails", "project": "work/photo-archive", "at": "…", "kind": "task", "reason": "not_useful"}]
```

`reason` is `not_useful`, `wrong_project` or missing. Recent `not_useful` ones in a project are shown to the model as examples of what not to add there.

## todos-links.json

```json
{"not_same": [["3f9a1c2e", "9b20d7aa"]], "related": [["work/ops-notes", "work/payroll-scripts"]]}
```

Each pair is sorted. Follow-ups in a project also see the open items of its related projects, so they don't add them again and can close them.

## Export

`sessionpulse todo export [--format md|csv|json] [-p PROJECT] [--status open|all]` prints the list; the board's **Export** menu downloads the same thing for the project you picked.

- **md**: grouped by project, with the kind, the detail, the quote and the resume command under each item.
- **csv**: `id, text, project, kind, state, created, done_at, detail, quote, duplicate_of, resume`. `state` is `open`, `doing`, `hold`, `archived` or `done`. A cell that starts with `=`, `+`, `-` or `@` gets a leading `'` so spreadsheets don't run it as a formula.
- **json**: the items as they are in `todos.json`.
