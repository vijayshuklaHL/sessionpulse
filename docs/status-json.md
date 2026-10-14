# status.json

`sessionpulse daemon` writes `$XDG_DATA_HOME/sessionpulse/status.json` (normally `~/.local/share/sessionpulse/status.json`) every `interval` seconds and deletes it when it exits cleanly. It is for panel widgets, such as the GNOME extension, that should show the state without talking to the dashboard server.

- Written atomically (temp file + rename), so readers never see half a file. A file monitor sees a create/rename on each update.
- Mode 0600. It holds project names but never the dashboard token.
- **Stale:** if `updated` is older than about `3 × interval`, the daemon was killed or the machine slept. Show "not running".
- **Missing:** the daemon isn't running.

```json
{
  "schema": 1,
  "version": "0.1.3",
  "updated": 1791990000000,
  "interval": 5,
  "pid": 12345,
  "port": 8765,
  "state": "busy",
  "open": 2,
  "working": 1,
  "sessions": [
    {"project": "~/work/api", "state": "busy", "started": 1791986400000},
    {"project": "~/work/web", "state": "idle", "started": 1791983000000}
  ],
  "today": {"sessions": 6, "active_hours": 3.5, "output_tokens": 120000},
  "todos_open": 4,
  "warnings": 0
}
```

| Field | Meaning |
|---|---|
| `schema` | Format version. Readers should ignore fields they don't know, and refuse a higher major `schema`. |
| `updated`, `started` | Milliseconds since the Unix epoch |
| `state` | `busy` if any session is working, `idle` if sessions are open but all idle, `none` if no sessions are open |
| `sessions` | Open sessions, most recently active first |
| `port` | The dashboard is at `http://127.0.0.1:<port>/`. To sign in, read the token from `token` in the same folder and open `/?token=<token>` once. |
