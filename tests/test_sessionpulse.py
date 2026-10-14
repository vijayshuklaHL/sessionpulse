"""Run with: python3 -m unittest discover -s tests"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sessionpulse import cli, config, followups, hook, install, learn, reader, server, status, todos, tray  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def entry(t, typ, **kw):
    return {"type": typ, "timestamp": t.isoformat().replace("+00:00", "Z"),
            "cwd": str(Path.home() / "work" / "demo"), "sessionId": SID, "version": "2.1.287", **kw}


def assistant(t, mid, out, model="claude-test-1", tool=False):
    content = [{"type": "tool_use", "name": "Bash"}] if tool else [{"type": "text", "text": "ok"}]
    return entry(t, "assistant", message={
        "id": mid, "role": "assistant", "model": model, "content": content,
        "usage": {"input_tokens": 10, "output_tokens": out,
                  "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}})


def user(t, text):
    return entry(t, "user", message={"role": "user", "content": text})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = config.Config(claude_dir=str(root / "claude"), data_dir=str(root / "data"),
                                 port=0)
        self.proj = self.cfg.claude / "projects" / "-home-demo"
        self.proj.mkdir(parents=True)
        (self.cfg.claude / "sessions").mkdir()
        reader._cache.clear()
        self.t0 = datetime(2026, 10, 13, 9, 0).astimezone()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, path=None):
        path = path or self.proj / f"{SID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        return path


class ReaderTests(Fixture):
    def test_usage_deduplicated_by_message_id(self):
        t = self.t0
        self.write([user(t, "fix the bug"),
                    assistant(t, "m1", 50), assistant(t, "m1", 50, tool=True),  # same response, two blocks
                    assistant(t + timedelta(minutes=1), "m2", 25)])
        s = reader.snapshot(self.cfg)["sessions"][0]
        self.assertEqual(s["usage"]["output"], 75)
        self.assertEqual(s["usage"]["cache_read"], 2000)
        self.assertEqual(s["tools"], 1)
        self.assertEqual(s["project"], "work/demo")

    def test_noise_is_not_a_prompt(self):
        t = self.t0
        self.write([user(t, "<command-name>/clear</command-name>"), user(t, "real question"),
                    user(t, "[Request interrupted by user]"), assistant(t, "m1", 5)])
        s = reader.snapshot(self.cfg)["sessions"][0]
        self.assertEqual(s["prompts"], 1)
        self.assertEqual(s["first_prompt"], "real question")

    def test_active_hours_split_on_gaps(self):
        t = self.t0
        stamps = [t, t + timedelta(minutes=20), t + timedelta(minutes=40),  # 40 min block
                  t + timedelta(hours=3)]                                    # lone stamp -> 15 min
        self.assertAlmostEqual(reader.active_hours(stamps), 55 / 60)

    def test_subagent_usage_added_to_parent(self):
        t = self.t0
        self.write([user(t, "go"), assistant(t, "m1", 10)])
        self.write([assistant(t, "s1", 7, model="claude-small")],
                   self.proj / SID / "subagents" / "agent-1.jsonl")
        s = reader.snapshot(self.cfg)["sessions"][0]
        self.assertEqual(s["usage"]["output"], 17)
        self.assertEqual(s["agents"], 1)

    def test_bad_lines_counted_and_warned(self):
        t = self.t0
        self.write([user(t, "hi"), assistant(t, "m1", 1)] + ["{not json"] * 5)
        h = reader.snapshot(self.cfg)["health"]
        self.assertEqual(h["bad_lines"], 5)
        self.assertTrue(any("could not be read" in w for w in h["warnings"]))

    def test_newer_cli_version_warns(self):
        t = self.t0
        row = user(t, "hi")
        row["version"] = "9.0.1"
        self.write([row, assistant(t, "m1", 1)])
        self.assertTrue(any("newer than" in w for w in reader.snapshot(self.cfg)["health"]["warnings"]))

    def test_states_live_exited_closed(self):
        t = self.t0
        self.write([user(t, "hi"), assistant(t, "m1", 1)])
        (self.cfg.claude / "sessions" / "1.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": SID, "status": "busy", "startedAt": 1}))
        self.assertEqual(reader.snapshot(self.cfg)["sessions"][0]["state"], "busy")

        (self.cfg.claude / "sessions" / "1.json").unlink()
        self.assertEqual(reader.snapshot(self.cfg)["sessions"][0]["state"], "closed")

        self.cfg.data.mkdir(parents=True)
        self.cfg.events_file.write_text(json.dumps(
            {"ts": t.isoformat(), "event": "SessionEnd", "session": SID, "reason": "logout"}) + "\n")
        s = reader.snapshot(self.cfg)["sessions"][0]
        self.assertEqual((s["state"], s["exit_reason"]), ("exited", "logout"))

    def test_dead_pid_is_not_live(self):
        t = self.t0
        self.write([user(t, "hi"), assistant(t, "m1", 1)])
        (self.cfg.claude / "sessions" / "1.json").write_text(json.dumps(
            {"pid": 2 ** 22 + 12345, "sessionId": SID, "status": "busy"}))
        self.assertEqual(reader.snapshot(self.cfg)["sessions"][0]["state"], "closed")

    def test_unexpected_live_file_warns(self):
        (self.cfg.claude / "sessions" / "1.json").write_text(json.dumps({"something": "else"}))
        h = reader.snapshot(self.cfg)["health"]
        self.assertEqual(h["live_unrecognised"], 1)

    def test_tray_summary(self):
        t = self.t0
        self.write([user(t, "hi"), assistant(t, "m1", 42)])
        (self.cfg.claude / "sessions" / "1.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": SID, "status": "idle"}))
        v = tray.summary(reader.snapshot(self.cfg), today=t.date().isoformat())
        self.assertEqual((v["icon"], len(v["live"]), v["today_output"]), ("idle", 1, 42))


class InstallTests(Fixture):
    def settings(self):
        return json.loads((self.cfg.claude / "settings.json").read_text())

    def test_install_is_idempotent_and_keeps_other_hooks(self):
        other = {"type": "command", "command": "echo mine"}
        (self.cfg.claude / "settings.json").write_text(json.dumps(
            {"model": "x", "hooks": {"SessionEnd": [{"hooks": [other]}]}}))
        self.assertTrue(install.install_hooks(self.cfg))
        self.assertFalse(install.install_hooks(self.cfg))
        s = self.settings()
        self.assertEqual(s["model"], "x")
        ends = [h for g in s["hooks"]["SessionEnd"] for h in g["hooks"]]
        self.assertIn(other, ends)
        self.assertEqual(sum(install.HOOK_MARK in h["command"] for h in ends), 1)
        self.assertEqual(install.hooks_installed(self.cfg), set(install.EVENTS))
        backups = list(self.cfg.claude.glob("settings.json.sessionpulse-backup-*"))
        self.assertEqual(len(backups), 1)

    def test_uninstall_removes_only_ours(self):
        other = {"type": "command", "command": "echo mine"}
        (self.cfg.claude / "settings.json").write_text(json.dumps(
            {"hooks": {"SessionEnd": [{"hooks": [other]}]}}))
        install.install_hooks(self.cfg)
        self.assertTrue(install.uninstall_hooks(self.cfg))
        self.assertEqual(self.settings(), {"hooks": {"SessionEnd": [{"hooks": [other]}]}})

    def test_invalid_settings_left_alone(self):
        p = self.cfg.claude / "settings.json"
        p.write_text("{ broken")
        with self.assertRaises(install.SettingsError):
            install.install_hooks(self.cfg)
        self.assertEqual(p.read_text(), "{ broken")

    def test_hook_appends_event(self):
        os.environ["XDG_DATA_HOME"] = str(Path(self.tmp.name) / "xdg")
        try:
            import importlib
            importlib.reload(config)
            hook.run(io.StringIO(json.dumps({"hook_event_name": "SessionEnd", "session_id": SID,
                                             "cwd": "/tmp", "reason": "prompt_input_exit"})))
            ev = json.loads((config.load().events_file).read_text())
            self.assertEqual((ev["event"], ev["reason"]), ("SessionEnd", "prompt_input_exit"))
            hook.run(io.StringIO("not json"))  # must not raise
        finally:
            del os.environ["XDG_DATA_HOME"]
            importlib.reload(config)


class ServerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.write([user(self.t0, "hi"), assistant(self.t0, "m1", 1)])
        self.srv = server.make_server(self.cfg)
        self.cfg.port = self.srv.server_address[1]
        self.srv.RequestHandlerClass = server.make_handler(self.cfg, config.token(self.cfg))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.cfg.port}{path}", headers=headers or {})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(req)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_requires_token(self):
        self.assertEqual(self.get("/api/data")[0], 403)
        self.assertEqual(self.get("/?token=wrong")[0], 403)

    def test_token_sets_cookie_then_data_flows(self):
        code, headers, _ = self.get(f"/?token={config.token(self.cfg)}")
        self.assertEqual(code, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        code, _, body = self.get("/api/data", {"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)["sessions"]), 1)

    def test_foreign_host_rejected(self):
        code, headers, _ = self.get(f"/?token={config.token(self.cfg)}")
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.get("/api/data", {"Cookie": cookie, "Host": "evil.example:80"})[0], 421)
        self.assertEqual(self.get("/api/data", {"Cookie": cookie, "Host": "127.0.0.1.evil.example:80"})[0], 421)
        self.assertEqual(self.get("/api/data", {"Cookie": cookie, "Host": "127.0.0.1"})[0], 421)

    def test_ssh_tunnel_on_another_local_port(self):
        tunnel = {"Host": "127.0.0.1:18765"}
        code, headers, _ = self.get(f"/?token={config.token(self.cfg)}", tunnel)
        self.assertEqual(code, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.get("/api/data", {"Cookie": cookie, **tunnel})[0], 200)
        add = {"op": "add", "text": "via tunnel"}
        self.assertEqual(self.post(add, {**tunnel, "Origin": "http://127.0.0.1:18765"})[0], 200)

    def test_brand_images_are_public_but_only_listed_files(self):
        code, headers, body = self.get("/brand/favicon.svg")
        self.assertEqual((code, headers["Content-Type"]), (200, "image/svg+xml"))
        self.assertTrue(body.startswith(b"<svg"))
        for bad in ("/brand/../server.py", "/brand/%2e%2e/server.py", "/brand/dashboard.html", "/brand/"):
            self.assertEqual(self.get(bad)[0], 404, bad)
        self.assertIn(b"/brand/session-pulse-none-animated.svg", self.get("/")[2])  # sign-in page

    def test_token_file_is_private(self):
        mode = (self.cfg.data / "token").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


    def post(self, body, headers=None, path="/api/todos"):
        code, h, _ = self.get(f"/?token={config.token(self.cfg)}")
        hdrs = {"Cookie": h["Set-Cookie"].split(";")[0], "Content-Type": "application/json"}
        hdrs.update(headers or {})
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.cfg.port}{path}", data=data,
                                     headers={k: v for k, v in hdrs.items() if v is not None})
        try:
            r = urllib.request.urlopen(req)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_todo_post_round_trip(self):
        code, body = self.post({"op": "add", "text": "ship it", "project": "demo"})
        self.assertEqual(code, 200)
        item = json.loads(body)[0]
        self.assertEqual((item["text"], item["project"], item["done_at"]), ("ship it", "demo", None))
        code, body = self.post({"op": "done", "id": item["id"]})
        self.assertTrue(json.loads(body)[0]["done_at"])
        self.assertEqual(self.post({"op": "nope"})[0], 400)

    def test_todo_post_guards(self):
        add = {"op": "add", "text": "x"}
        self.assertEqual(self.post(add, {"Cookie": None})[0], 403)
        self.assertEqual(self.post(add, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post(add, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post(add, {"Origin": "http://127.0.0.1:3000"})[0], 403)  # another local page
        self.assertEqual(self.post(b"not json")[0], 400)
        self.assertEqual(todos.load(self.cfg), [])
        self.assertEqual(self.post(add, {"Origin": f"http://127.0.0.1:{self.cfg.port}"})[0], 200)


class TodoTests(Fixture):
    def test_add_done_undo_edit_delete(self):
        items = todos.apply(self.cfg, {"op": "add", "text": "  write   tests  "})
        i = items[0]["id"]
        self.assertEqual(items[0]["text"], "write tests")
        self.assertTrue(todos.apply(self.cfg, {"op": "done", "id": i})[0]["done_at"])
        self.assertIsNone(todos.apply(self.cfg, {"op": "undo", "id": i})[0]["done_at"])
        self.assertEqual(todos.apply(self.cfg, {"op": "edit", "id": i, "text": "b"})[0]["text"], "b")
        self.assertEqual(todos.apply(self.cfg, {"op": "delete", "id": i}), [])
        mode = (self.cfg.data / "todos.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_bulk_dismiss_remembers_session_items(self):
        todos.merge_followups(self.cfg, {"id": "s", "project": "p"}, [{"text": "a", "kind": "task"},
                              {"text": "b", "kind": "task"}], [])
        ids = [i["id"] for i in todos.load(self.cfg)]
        self.assertEqual(todos.apply(self.cfg, {"op": "delete", "ids": ids}), [])
        self.assertEqual(sorted(d["text"] for d in todos.dismissed(self.cfg)), ["a", "b"])
        self.assertEqual(todos.merge_followups(self.cfg, {"id": "s2", "project": "p"}, [{"text": "a"}], []), (0, 0))

    def test_board_moves(self):
        i = todos.apply(self.cfg, {"op": "add", "text": "x"})[0]["id"]
        it = todos.apply(self.cfg, {"op": "set", "id": i, "kind": "decision", "doing": True})[0]
        self.assertEqual((it["kind"], it["doing"]), ("decision", True))
        it = todos.apply(self.cfg, {"op": "set", "id": i, "done": True})[0]
        self.assertTrue(it["done_at"])
        self.assertFalse(it["doing"])
        it = todos.apply(self.cfg, {"op": "set", "id": i, "done": False, "kind": None})[0]
        self.assertEqual((it["done_at"], it["kind"]), (None, None))
        with self.assertRaises(ValueError):
            todos.apply(self.cfg, {"op": "set", "id": i, "kind": "nope"})

    def test_bad_ops_rejected(self):
        for op in ({"op": "add", "text": "  "}, {"op": "done", "id": "missing"}, {"op": "x"}, []):
            with self.assertRaises(ValueError):
                todos.apply(self.cfg, op)

    def test_corrupt_file_reads_as_empty(self):
        self.cfg.data.mkdir(parents=True)
        (self.cfg.data / "todos.json").write_text("{oops")
        self.assertEqual(todos.load(self.cfg), [])

    def test_cli(self):
        out = io.StringIO()
        with unittest.mock.patch.object(config, "load", return_value=self.cfg), \
             unittest.mock.patch("sys.stdout", out):
            cli.main(["todo", "add", "call", "Sam", "-p", "ops"])
            i = todos.load(self.cfg)[0]["id"]
            cli.main(["todo", "done", i])
            cli.main(["todo", "list-done"])
        self.assertIn("call Sam  [ops]  done", out.getvalue())


class StatusTests(Fixture):
    def live_session(self):
        t = self.t0
        self.write([user(t, "hi"), assistant(t, "m1", 42)])
        (self.cfg.claude / "sessions" / "1.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": SID, "status": "busy", "startedAt": 1234}))
        return t.date().isoformat()

    def test_status_file(self):
        day = self.live_session()
        todos.apply(self.cfg, {"op": "add", "text": "ship it"})
        data = status.write_now(self.cfg, today=day)
        on_disk = json.loads(status.path(self.cfg).read_text())
        self.assertEqual(on_disk, data)
        self.assertEqual((data["schema"], data["state"], data["open"], data["working"]), (1, "busy", 1, 1))
        self.assertEqual(data["sessions"][0]["state"], "busy")
        self.assertEqual(data["sessions"][0]["started"], 1234)
        self.assertEqual(data["today"]["output_tokens"], 42)
        self.assertEqual(data["todos_open"], 1)
        self.assertEqual(status.path(self.cfg).stat().st_mode & 0o777, 0o600)
        self.assertNotIn(config.token(self.cfg), status.path(self.cfg).read_text())
        self.assertLess(status.age(self.cfg), 5)
        self.assertEqual(list(self.cfg.data.glob(".status.*")), [])  # no temp files left

    def test_no_sessions_and_removal(self):
        data = status.write_now(self.cfg)
        self.assertEqual((data["state"], data["open"], data["sessions"]), ("none", 0, []))
        status.remove(self.cfg)
        status.remove(self.cfg)  # twice is fine
        self.assertIsNone(status.age(self.cfg))

    def test_daemon_loop_writes_then_cleans_up(self):
        self.live_session()
        stop, seen = threading.Event(), []

        def watch():
            for _ in range(200):
                if status.path(self.cfg).exists():
                    seen.append(json.loads(status.path(self.cfg).read_text())["open"])
                    break
                stop.wait(0.01)
            stop.set()

        threading.Thread(target=watch).start()
        with unittest.mock.patch.object(status, "INTERVAL", 0.02):
            status.run(self.cfg, stop)
        self.assertEqual(seen, [1])
        self.assertFalse(status.path(self.cfg).exists())

    def test_daemon_command_runs_without_icon(self):
        with unittest.mock.patch.object(config, "load", return_value=self.cfg), \
             unittest.mock.patch.object(cli.signal, "signal"), \
             unittest.mock.patch.object(cli.followups, "start_worker"), \
             unittest.mock.patch.object(cli.status, "run") as run, \
             unittest.mock.patch("sys.stdout", io.StringIO()) as out:
            cli.main(["daemon"])
        run.assert_called_once()
        self.assertIn("status.json", out.getvalue())

    def test_install_daemon_autostart(self):
        target = Path(self.tmp.name) / "autostart" / "sessionpulse.desktop"
        with unittest.mock.patch.object(install, "AUTOSTART", target):
            install.enable_autostart("daemon")
        self.assertIn("-m sessionpulse daemon", target.read_text())


class HeadlessTests(Fixture):
    def run_cli(self, argv, env):
        out = io.StringIO()
        with unittest.mock.patch.object(config, "load", return_value=self.cfg), \
             unittest.mock.patch.dict(os.environ, env, clear=False), \
             unittest.mock.patch.object(sys, "platform", "linux"), \
             unittest.mock.patch.object(cli.webbrowser, "open", return_value=True) as wb, \
             unittest.mock.patch("sys.stdout", out):
            for k in ("DISPLAY", "WAYLAND_DISPLAY"):
                if k not in env:
                    os.environ.pop(k, None)
            cli.main(argv)
        return out.getvalue(), wb

    def test_open_without_desktop_prints_tunnel_link(self):
        out, wb = self.run_cli(["open"], {})
        wb.assert_not_called()
        self.assertIn("ssh -N -L", out)
        self.assertIn("/?token=", out)
        self.assertIn("isn't running", out)

    def test_open_with_desktop_uses_browser(self):
        out, wb = self.run_cli(["open"], {"DISPLAY": ":0"})
        wb.assert_called_once()
        self.assertNotIn("ssh -N -L", out)

    def test_doctor_without_desktop_explains_server_use(self):
        out, _ = self.run_cli(["doctor"], {})
        self.assertIn("no desktop session", out)
        self.assertIn("sessionpulse serve", out)


class LearnTests(Fixture):
    def sessions(self, n=6, **kw):
        now = datetime.now().astimezone()
        base = {"cwd": str(self.cfg.claude), "hours": 1, "prompts": 5, "first_prompt": "please refactor the parser module",
                "end": now.isoformat(), "agents": 0, "tool_names": {}}
        return [{**base, **kw} for _ in range(n)]

    def test_too_few_sessions_gives_library_only(self):
        out = learn.suggest(self.cfg, self.sessions(2))
        self.assertEqual(out["suggestions"], [])
        self.assertTrue(out["library"])

    def test_habits_drive_suggestions(self):
        ids = {s["id"] for s in learn.suggest(self.cfg, self.sessions(prompts=50, first_prompt="hii"))["suggestions"]}
        self.assertTrue({"memory", "context", "prompting", "plan-mode", "subagents", "skills", "mcp"} <= ids)

    def test_used_features_become_strengths(self):
        (self.cfg.claude / "CLAUDE.md").write_text("x")
        used = {"ExitPlanMode": 1, "Agent": 1, "Skill": 1, "mcp__x__y": 1}
        out = learn.suggest(self.cfg, self.sessions(tool_names=used))
        self.assertEqual([s["id"] for s in out["suggestions"]], [])
        self.assertEqual(len(out["strengths"]), 5)

    def test_library_links_are_https_and_unique(self):
        ids = [r["id"] for r in learn.LIBRARY]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(r["url"].startswith("https://") for r in learn.LIBRARY))

    def test_tool_names_counted(self):
        self.write([user(self.t0, "hi"), assistant(self.t0, "m1", 1, tool=True)])
        s = reader.snapshot(self.cfg)["sessions"][0]
        self.assertEqual(s["tool_names"], {"Bash": 1})


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\\n")
resolved = [l.split("]")[0].split("[")[1] for l in prompt.splitlines() if l.startswith("- [") and "old item" in l]
print(json.dumps({"is_error": False, "structured_output": {"items": [
    {"text": "Ask the vendor for the usage breakdown", "kind": "external", "detail": "invoice is higher than our records"},
    {"text": "Pick a brand", "kind": "bogus"}], "resolved": resolved}}))
"""


class FollowupTests(Fixture):
    def setUp(self):
        super().setUp()
        old = datetime.now().astimezone() - timedelta(hours=2)
        self.write([user(old, "draft the vendor message"), assistant(old, "m1", 5),
                    entry(old, "assistant", message={"id": "m2", "role": "assistant", "model": "claude-test-1",
                          "content": [{"type": "text", "text": "Still open: ask the vendor."}]})])
        fake = Path(self.tmp.name) / "fake-claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        self.cfg.claude_command = str(fake)
        self.log = Path(self.tmp.name) / "calls.jsonl"
        os.environ["FAKE_LOG"] = str(self.log)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_digest_keeps_prompts_and_replies_not_noise(self):
        d = followups.digest(self.proj / f"{SID}.jsonl", "demo")
        self.assertIn("draft the vendor message", d)
        self.assertIn("Still open: ask the vendor.", d)
        self.assertTrue(d.startswith("Project: demo"))

    def test_scan_adds_items_once_and_runs_claude_safely(self):
        self.assertEqual(followups.scan(self.cfg), (1, 2, 0))
        items = todos.load(self.cfg)
        self.assertEqual([i["kind"] for i in items], ["external", "task"])  # unknown kind -> task
        self.assertEqual(items[0]["session"], SID)
        argv = self.calls()[0]["argv"]
        for flag in ("--no-session-persistence", "--json-schema", "--system-prompt"):
            self.assertIn(flag, argv)
        self.assertIn('"disableAllHooks": true', argv[argv.index("--settings") + 1])
        self.assertEqual(followups.scan(self.cfg), (0, 0, 0))  # unchanged transcript isn't re-read
        self.assertEqual(followups.scan(self.cfg, force=True)[1], 0)  # nothing duplicated

    def test_dismissed_items_stay_dismissed(self):
        followups.scan(self.cfg)
        first = todos.load(self.cfg)[0]
        todos.apply(self.cfg, {"op": "delete", "id": first["id"]})
        followups.scan(self.cfg, force=True)
        self.assertNotIn(first["text"], [i["text"] for i in todos.load(self.cfg)])
        self.assertIn(first["text"], self.calls()[-1]["prompt"])

    def test_later_session_closes_open_item(self):
        todos.merge_followups(self.cfg, {"id": "x", "project": "work/demo"}, [{"text": "old item", "kind": "task"}], [])
        followups.scan(self.cfg)
        old = next(i for i in todos.load(self.cfg) if i["text"] == "old item")
        self.assertTrue(old["done_at"])
        self.assertEqual(old["closed_by"], SID)

    def test_recent_session_waits_until_quiet(self):
        self.write([user(datetime.now().astimezone(), "hi"), assistant(datetime.now().astimezone(), "m1", 1)])
        self.assertEqual(followups.scan(self.cfg), (0, 0, 0))
        self.assertEqual(followups.scan(self.cfg, idle_minutes=0)[0], 1)

    def test_worker_off_by_default(self):
        self.assertIsNone(followups.start_worker(self.cfg))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


if __name__ == "__main__":
    unittest.main()
