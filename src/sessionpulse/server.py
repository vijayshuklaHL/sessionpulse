"""The dashboard's HTTP server.

Transcripts contain everything typed into Claude Code, so the server:
  * binds to 127.0.0.1 only;
  * rejects any Host header other than 127.0.0.1/localhost, which stops
    DNS-rebinding pages in your browser from reading it (any port, so an SSH
    tunnel such as -L 18765:127.0.0.1:8765 works);
  * needs a token (from <data_dir>/token). Open /?token=… once, via
    `sessionpulse open` or the panel menu, and it is kept as an HttpOnly,
    SameSite=Strict cookie, so other local users and other sites can't read it.
"""
import hmac
import json
from datetime import date
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import config, followups, reader, todos

COOKIE = "sessionpulse"

DENIED = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session Pulse</title><link rel="icon" href="/brand/favicon.svg">
<body style="margin:0;min-height:100vh;background:#090c12;color:#eef0f2;font:15px system-ui,sans-serif">
<main style="max-width:640px;margin:0 auto;padding:10vh 16px 32px">
<img src="/brand/session-pulse-none-animated.svg" alt="" style="width:100%;height:auto;border-radius:14px;display:block">
<h1 style="font-size:20px;margin:28px 0 8px">Session Pulse needs its access link</h1>
<p style="color:#8695a5;margin:0">Open the dashboard from the panel icon, or run
<code style="color:#eef0f2">sessionpulse open</code> in a terminal.</p></main>"""


def make_handler(cfg: config.Config, tok: str):
    def local_host(value):
        name, _, port = (value or "").rpartition(":")
        return name in ("127.0.0.1", "localhost") and port.isdigit()

    page = resources.files("sessionpulse").joinpath("static/dashboard.html").read_bytes()
    # Illustrations and the favicon: public (the sign-in page shows one), and only these files.
    brand = {f.name: f.read_bytes() for f in resources.files("sessionpulse").joinpath("static/brand").iterdir()
             if f.name.endswith(".svg")}

    class Handler(BaseHTTPRequestHandler):
        server_version = "SessionPulse"

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype, headers=(), cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            got = c[COOKIE].value if COOKIE in c else ""
            return hmac.compare_digest(got.encode(), tok.encode())

        def do_GET(self):
            if not local_host(self.headers.get("Host")):
                return self.send(421, b"wrong host", "text/plain")
            url = urlparse(self.path)
            if url.path.startswith("/brand/"):
                name = url.path.removeprefix("/brand/")
                if name not in brand:
                    return self.send(404, b"not found", "text/plain")
                return self.send(200, brand[name], "image/svg+xml", cache="max-age=86400")
            given = parse_qs(url.query).get("token", [""])[0]
            if given:
                if not hmac.compare_digest(given.encode(), tok.encode()):
                    return self.send(403, DENIED, "text/html; charset=utf-8")
                cookie = f"{COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"
                return self.send(303, b"", "text/plain", [("Set-Cookie", cookie), ("Location", "/")])
            if not self.authed():
                return self.send(403, DENIED, "text/html; charset=utf-8")
            if url.path == "/api/data":
                with reader.lock:
                    data = reader.snapshot(cfg)
                data["followups"] = {"enabled": cfg.followups, **followups.status,
                                     "week": followups.usage_totals(cfg, 7), "failing": len(followups.failing(cfg))}
                ids = {s["id"] for s in data["sessions"]}
                data["briefs"] = {k: v for k, v in followups.load_briefs(cfg).items() if k in ids}
                return self.send(200, json.dumps(data).encode(), "application/json")
            if url.path == "/api/excerpt":
                return self.excerpt(parse_qs(url.query))
            if url.path == "/api/export":
                return self.export(parse_qs(url.query))
            if url.path in ("/", "/index.html"):
                return self.send(200, page, "text/html; charset=utf-8")
            self.send(404, b"not found", "text/plain")

        def excerpt(self, q):
            """A few turns of a session's transcript around one turn: where a follow-up came from."""
            sid, uuid = q.get("session", [""])[0], q.get("uuid", [""])[0]
            if not sid or not uuid or len(sid) > 64 or len(uuid) > 64:
                return self.send(400, b'{"error": "session and uuid are required"}', "application/json")
            with reader.lock:
                sess = next((s for s in reader.snapshot(cfg)["sessions"] if s["id"] == sid), None)
            try:
                turns = followups.excerpt(sess["transcript"], uuid) if sess and sess.get("transcript") else None
            except OSError:
                turns = None
            if turns is None:
                return self.send(404, b'{"error": "that part of the transcript is gone"}', "application/json")
            return self.send(200, json.dumps({"session": sid, "project": sess.get("project"), "cwd": sess.get("cwd"),
                                              "turns": turns}).encode(), "application/json")

        def export(self, q):
            """The to-do list as a file to save (it's only formatted here, never sent anywhere)."""
            fmt, status = q.get("format", ["md"])[0], q.get("status", ["open"])[0]
            project = q["project"][0] if q.get("project") else None
            try:
                body = todos.export(todos.load(cfg), fmt, project, status)
            except ValueError as exc:
                return self.send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            ctype = {"md": "text/markdown", "csv": "text/csv", "json": "application/json"}[fmt]
            name = f"sessionpulse-todos-{date.today().isoformat()}.{fmt}"
            return self.send(200, body.encode(), f"{ctype}; charset=utf-8",
                             [("Content-Disposition", f'attachment; filename="{name}"')])

        def do_POST(self):
            # Writes need, on top of the cookie: a JSON content type (a plain cross-site
            # form can't send one without a CORS preflight, which we never answer) and,
            # when the browser sends one, an Origin equal to the address the request
            # went to, so another local page (e.g. 127.0.0.1:3000) can't post here.
            if not local_host(self.headers.get("Host")):
                return self.send(421, b"wrong host", "text/plain")
            if not self.authed():
                return self.send(403, b"forbidden", "text/plain")
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                return self.send(403, b"bad origin", "text/plain")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self.send(415, b"expected application/json", "text/plain")
            if urlparse(self.path).path != "/api/todos":
                return self.send(404, b"not found", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= 16384:
                return self.send(413, b"bad length", "text/plain")
            try:
                items = todos.apply(cfg, json.loads(self.rfile.read(n)))
            except ValueError as exc:
                return self.send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            self.send(200, json.dumps(items).encode(), "application/json")

    return Handler


def make_server(cfg: config.Config):
    return ThreadingHTTPServer(("127.0.0.1", cfg.port), make_handler(cfg, config.token(cfg)))


def open_url(cfg: config.Config):
    return f"{cfg.url}/?token={config.token(cfg)}"
