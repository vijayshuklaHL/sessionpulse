"""Run with: python3 -m unittest discover -s tests"""
import csv
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

    def test_export_downloads_the_list(self):
        for text, project in (('=HYPERLINK("x")', "a"), ("ship it", "b")):
            todos.apply(self.cfg, {"op": "add", "text": text, "project": project})
        cookie = {"Cookie": self.get(f"/?token={config.token(self.cfg)}")[1]["Set-Cookie"].split(";")[0]}
        code, head, body = self.get("/api/export?format=csv&project=a", cookie)
        self.assertEqual(code, 200)
        self.assertIn("attachment", head["Content-Disposition"])
        self.assertTrue(head["Content-Type"].startswith("text/csv"))
        body = body.decode() if isinstance(body, bytes) else body
        self.assertIn("'=HYPERLINK", body)  # a spreadsheet won't run it
        self.assertNotIn("ship it", body)
        self.assertEqual(self.get("/api/export?format=exe", cookie)[0], 400)
        self.assertEqual(self.get("/api/export")[0], 403)

    def test_data_carries_briefs_of_known_sessions(self):
        brief = {"goal": "Ship the recipe site", "done": [], "open": [], "blocked": "", "next": "Deploy"}
        followups._save_brief(self.cfg, {"id": SID, "project": "p"}, brief, 4)
        followups._save_brief(self.cfg, {"id": "gone", "project": "p"}, brief, 2)
        cookie = self.get(f"/?token={config.token(self.cfg)}")[1]["Set-Cookie"].split(";")[0]
        briefs = json.loads(self.get("/api/data", {"Cookie": cookie})[2])["briefs"]
        self.assertEqual(list(briefs), [SID])
        self.assertEqual((briefs[SID]["brief"]["next"], briefs[SID]["turns"]), ("Deploy", 4))

    def test_excerpt_shows_turns_around_one(self):
        rows = [user(self.t0, f"prompt {n}") | {"uuid": f"u{n}"} for n in range(6)]
        rows[3]["message"]["content"] = "my DB_PASSWORD=hunter2 is set"
        self.write(rows)
        reader._cache.clear()
        cookie = self.get(f"/?token={config.token(self.cfg)}")[1]["Set-Cookie"].split(";")[0]
        code, _, body = self.get(f"/api/excerpt?session={SID}&uuid=u3", {"Cookie": cookie})
        self.assertEqual(code, 200)
        turns = json.loads(body)["turns"]
        self.assertEqual([t["uuid"] for t in turns], ["u1", "u2", "u3", "u4", "u5"])
        self.assertEqual([t["hit"] for t in turns].index(True), 2)
        self.assertEqual(turns[2]["text"], "my DB_PASSWORD=[secret] is set")
        self.assertEqual(self.get(f"/api/excerpt?session={SID}&uuid=nope", {"Cookie": cookie})[0], 404)
        self.assertEqual(self.get("/api/excerpt?session=x", {"Cookie": cookie})[0], 400)
        self.assertEqual(self.get(f"/api/excerpt?session={SID}&uuid=u3")[0], 403)  # needs the token

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

    def test_reworded_followups_skipped(self):
        merge = todos.merge_followups
        merge(self.cfg, {"id": "s", "project": "p"},
              [{"text": "Put Grafana alert rules on no data and execution error"}], [])
        self.assertEqual(merge(self.cfg, {"id": "s2", "project": "p"},
                               [{"text": "Check that Grafana alert rules fire on no data and on execution error"},
                                {"text": "Disable Telnet on the Credo routers"},
                                {"text": "Disable WAN management on the Credo routers"}], []), (2, 0))
        # the same words in another project are a different to-do
        self.assertEqual(merge(self.cfg, {"id": "s3", "project": "q"},
                               [{"text": "Check Grafana alert rules on no data and execution error"}], []), (1, 0))

    def test_merge_duplicates(self):
        a = todos.apply(self.cfg, {"op": "add", "text": "old wording"})[-1]["id"]
        b = todos.apply(self.cfg, {"op": "add", "text": "new wording"})[-1]["id"]
        c = todos.apply(self.cfg, {"op": "add", "text": "in progress"})[-1]["id"]
        todos.apply(self.cfg, {"op": "set", "id": c, "doing": True})
        self.assertEqual(todos.merge_duplicates(self.cfg, [[a, b, c], [a, "missing"]]), 2)
        by = {i["id"]: i for i in todos.load(self.cfg)}
        self.assertTrue(todos.is_open(by[c]))  # the one in progress is kept
        self.assertEqual([(by[x]["status"], by[x]["duplicate_of"]) for x in (a, b)], [("archived", c)] * 2)
        it = next(i for i in todos.apply(self.cfg, {"op": "set", "id": a, "status": None}) if i["id"] == a)
        self.assertNotIn("duplicate_of", it)
        self.assertEqual(todos.merge_duplicates(self.cfg, [[b, c]]), 0)  # b is already archived

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

    def test_hold_and_archive(self):
        i = todos.apply(self.cfg, {"op": "add", "text": "x"})[0]["id"]
        it = todos.apply(self.cfg, {"op": "set", "id": i, "doing": True})[0]
        it = todos.apply(self.cfg, {"op": "set", "id": i, "done": False, "status": "hold"})[0]
        self.assertEqual((it["status"], it["doing"], it["done_at"]), ("hold", False, None))
        self.assertFalse(todos.is_open(it))
        it = todos.apply(self.cfg, {"op": "set", "id": i, "status": "archived"})[0]
        self.assertEqual(it["status"], "archived")
        it = todos.apply(self.cfg, {"op": "set", "id": i, "done": False, "status": None})[0]
        self.assertNotIn("status", it)
        self.assertTrue(todos.is_open(it))
        todos.apply(self.cfg, {"op": "set", "id": i, "status": "hold"})
        it = todos.apply(self.cfg, {"op": "done", "id": i})[0]
        self.assertNotIn("status", it)  # finishing a held item un-parks it
        with self.assertRaises(ValueError):
            todos.apply(self.cfg, {"op": "set", "id": i, "status": "someday"})

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


class ExportTests(Fixture):
    def setUp(self):
        super().setUp()
        todos.merge_followups(self.cfg, {"id": SID, "project": "work/ops", "cwd": "/home/demo/ops"},
                              [{"text": "Renew the NAS warranty", "kind": "external",
                                "evidence": {"quote": "the NAS warranty runs out in May"}}], [])
        for text, project in (("Water the plants", None), ("Old idea", "work/ops"), ("Shipped thing", "work/ops")):
            todos.apply(self.cfg, {"op": "add", "text": text, "project": project})
        ids = {i["text"]: i["id"] for i in todos.load(self.cfg)}
        todos.apply(self.cfg, {"op": "set", "id": ids["Old idea"], "status": "hold"})
        todos.apply(self.cfg, {"op": "done", "id": ids["Shipped thing"]})

    def test_markdown_groups_by_project_with_resume_commands(self):
        md = todos.export(todos.load(self.cfg), "md")
        self.assertLess(md.index("## No project"), md.index("## work/ops"))
        self.assertIn("- [ ] Renew the NAS warranty *(external)*\n  > the NAS warranty runs out in May\n"
                      f"  `cd /home/demo/ops && claude --resume {SID}`", md)
        self.assertNotIn("Old idea", md)
        full = todos.export(todos.load(self.cfg), "md", status="all")
        self.assertIn("- [ ] Old idea *(on hold)*", full)
        self.assertIn("- [x] Shipped thing (done ", full)

    def test_json_csv_and_filters(self):
        items = todos.load(self.cfg)
        self.assertEqual([i["text"] for i in json.loads(todos.export(items, "json", project=""))], ["Water the plants"])
        rows = list(csv.reader(io.StringIO(todos.export(items, "csv", "work/ops", "all"))))
        self.assertEqual(rows[0][:5], ["id", "text", "project", "kind", "state"])
        self.assertEqual(sorted(r[4] for r in rows[1:]), ["done", "hold", "open"])
        with self.assertRaises(ValueError):
            todos.export(items, "pdf")

    def test_cli_export(self):
        with unittest.mock.patch.object(config, "load", return_value=self.cfg), \
             unittest.mock.patch("sys.stdout", io.StringIO()) as out:
            cli.main(["todo", "export", "--format", "csv", "-p", "work/ops"])
        self.assertIn("Renew the NAS warranty", out.getvalue())
        self.assertNotIn("Water the plants", out.getvalue())


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

    def test_serve_on_busy_port_says_so(self):
        with unittest.mock.patch.object(config, "load", return_value=self.cfg), \
             unittest.mock.patch.object(cli.server, "make_server", side_effect=OSError(98, "Address already in use")), \
             self.assertRaises(SystemExit) as stop:
            cli.main(["serve"])
        self.assertIn(f"port {self.cfg.port}", str(stop.exception))
        self.assertIn("config.toml", str(stop.exception))

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
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "crash":
    print("Traceback: something broke")
    sys.exit(1)
if mode == "limit":
    print(json.dumps({"is_error": True, "result": "Claude AI usage limit reached|1761868800",
                      "usage": {"input_tokens": 0, "output_tokens": 0}}))
    sys.exit(1)
resolved = [l.split("]")[0].split("[")[1] for l in prompt.splitlines() if l.startswith("- [") and "old item" in l]
items = [{"text": "Ask the vendor for the usage breakdown", "kind": "external", "detail": "invoice is higher than our records"},
         {"text": "Pick a brand", "kind": "bogus"}]
if mode == "quotes":
    items[0]["quote"] = "Still open: ask the vendor. Key: sk-ant-abcdefghijklmnopqrstuvwx"
    items[1]["quote"] = "We agreed to pick the blue brand"
    resolved = [{"id": r, "quote": "draft the vendor message", "ref": 1} for r in resolved]
if mode == "across":
    print(json.dumps({"is_error": False, "usage": {}, "structured_output": {"same": [1, 7]}}))
    sys.exit(0)
if mode == "junk":
    items = [5, "text", {"text": ""}, {"text": ["x"]}, {"kind": "task"}, {"text": "Call the bank", "detail": 7}]
    resolved = resolved + ["not-shown", 3, None, os.environ.get("FAKE_EXTRA_RESOLVED")]
brief = {"goal": "Draft the vendor message", "done": ["drafted it"], "open": ["ask the vendor"],
         "blocked": "", "next": "send it"}
print(json.dumps({"is_error": False, "total_cost_usd": 0.0025, "duration_ms": 1500,
                  "usage": {"input_tokens": 900, "output_tokens": 60, "cache_read_input_tokens": 100},
                  "structured_output": {"items": items, "resolved": resolved, "brief": brief}}))
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
        os.environ["FAKE_MODE"] = "ok"

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

    def test_usage_recorded_per_day_and_session(self):
        followups.scan(self.cfg)
        week = followups.usage_totals(self.cfg, 7)
        self.assertEqual((week["calls"], week["tokens"], week["output_tokens"]), (1, 1060, 60))
        self.assertAlmostEqual(week["cost_usd"], 0.0025)
        self.assertEqual(followups.load_state(self.cfg)[SID]["tokens"], 1060)
        followups.scan(self.cfg, force=True)
        self.assertEqual(followups.usage_totals(self.cfg, 1)["calls"], 2)

    def test_failing_session_backs_off_then_waits_for_growth(self):
        os.environ["FAKE_MODE"] = "crash"
        with self.assertRaises(RuntimeError):
            followups.scan(self.cfg)
        e = followups.load_state(self.cfg)[SID]
        self.assertEqual(e["fails"], 1)
        self.assertIn("something broke", e["last_error"])
        self.assertEqual(followups.scan(self.cfg), (0, 0, 0))  # backing off: not even tried
        self.assertEqual(len(self.calls()), 1)
        st = followups.load_state(self.cfg)
        st[SID]["next_try"] = "2000-01-01T00:00:00+00:00"
        followups._save_state(self.cfg, st)
        with self.assertRaises(RuntimeError):
            followups.scan(self.cfg)  # due again: 2nd failure
        st = followups.load_state(self.cfg)
        st[SID]["next_try"] = "2000-01-01T00:00:00+00:00"
        followups._save_state(self.cfg, st)
        with self.assertRaises(RuntimeError):
            followups.scan(self.cfg)  # 3rd failure: no more tries until it grows
        e = followups.load_state(self.cfg)[SID]
        self.assertEqual((e["fails"], "next_try" in e), (3, False))
        self.assertEqual(followups.scan(self.cfg), (0, 0, 0))
        self.assertEqual([s for s, _ in followups.failing(self.cfg)], [SID])
        os.environ["FAKE_MODE"] = "ok"
        self.assertEqual(followups.scan(self.cfg, session=SID[:8])[0], 1)  # asked for by name: tried
        self.assertNotIn("fails", followups.load_state(self.cfg)[SID])
        self.assertEqual(followups.failing(self.cfg), [])

    def test_grown_session_is_retried_after_failing(self):
        os.environ["FAKE_MODE"] = "crash"
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                followups.scan(self.cfg, force=True)
        os.environ["FAKE_MODE"] = "ok"
        self.assertEqual(followups.scan(self.cfg), (0, 0, 0))
        with open(self.proj / f"{SID}.jsonl", "a") as f:
            f.write(json.dumps(user(datetime.now().astimezone() - timedelta(hours=1), "one more thing")) + "\n")
        self.assertEqual(followups.scan(self.cfg)[0], 1)

    def test_usage_limit_blames_no_session(self):
        os.environ["FAKE_MODE"] = "limit"
        with self.assertRaises(followups.NotReady):
            followups.scan(self.cfg)
        self.assertEqual(followups.failing(self.cfg), [])
        self.assertEqual(followups.usage_totals(self.cfg, 1)["calls"], 1)

    def test_missing_claude_blames_no_session(self):
        self.cfg.claude_command = str(Path(self.tmp.name) / "nope")
        with unittest.mock.patch.object(followups.shutil, "which", return_value=None), \
                unittest.mock.patch.object(followups.Path, "home", return_value=Path(self.tmp.name)):
            with self.assertRaises(RuntimeError):
                followups.scan(self.cfg)
        self.assertEqual(followups.load_state(self.cfg), {})

    def test_malformed_reply_is_cleaned(self):
        todos.merge_followups(self.cfg, {"id": "x", "project": "work/demo"}, [{"text": "old item", "kind": "task"}], [])
        todos.merge_followups(self.cfg, {"id": "y", "project": "elsewhere"}, [{"text": "far item", "kind": "task"}], [])
        far = next(i for i in todos.load(self.cfg) if i["text"] == "far item")
        os.environ["FAKE_MODE"] = "junk"
        with unittest.mock.patch.dict(os.environ, {"FAKE_EXTRA_RESOLVED": far["id"]}):
            self.assertEqual(followups.scan(self.cfg), (1, 1, 1))
        new = next(i for i in todos.load(self.cfg) if i["text"] == "Call the bank")
        self.assertEqual((new["kind"], new["detail"]), ("task", "7"))
        self.assertFalse(next(i for i in todos.load(self.cfg) if i["text"] == "far item")["done_at"])

    def test_digest_numbers_turns_for_quoting(self):
        turns = []
        d = followups.digest(self.proj / f"{SID}.jsonl", "demo", turns=turns)
        self.assertIn("### #1 USER", d)
        self.assertIn("### #2 CLAUDE", d)
        self.assertEqual([(t["n"], t["role"]) for t in turns], [(1, "user"), (2, "assistant"), (3, "assistant")])

    def test_quotes_are_checked_against_the_transcript(self):
        turns = [{"n": 1, "role": "user", "uuid": "a", "at": "t1", "text": "Please don’t ship it yet.\nI'll ask the vendor first."},
                 {"n": 2, "role": "assistant", "uuid": "b", "at": "t2", "text": "Sure, I'll wait for the vendor's answer."}]
        self.assertEqual(followups.locate("I'll ask the vendor first", turns)["uuid"], "a")
        self.assertEqual(followups.locate('"Please don\'t ship it yet. I\'ll ask"', turns)["uuid"], "a")  # quotes, spaces
        self.assertEqual(followups.locate("Please don't ship ... the vendor first", turns)["uuid"], "a")
        self.assertIsNone(followups.locate("I promised to call the vendor tomorrow", turns))  # made up
        self.assertIsNone(followups.locate("vendor", turns))  # too short to mean anything
        self.assertIsNone(followups.locate("ship it yet ... the vendor's answer", turns))  # pieces from two turns
        ev = followups.evidence({"quote": "I'll wait for the vendor's answer", "ref": 2}, turns)
        self.assertEqual((ev["uuid"], ev["role"], ev["at"]), ("b", "assistant", "t2"))
        self.assertIsNone(followups.evidence({"quote": 5}, turns))

    def test_secrets_redacted(self):
        r = todos.redact
        self.assertEqual(r("use ghp_abcdefghijklmnopqrstuvwxyz0123 now"), "use [secret] now")
        self.assertEqual(r("DB_PASSWORD=hunter2 and api_key: abc123"), "DB_PASSWORD=[secret] and api_key: [secret]")
        self.assertEqual(r("key sk-ant-api03-abcdefghijklmnopqrstu"), "key [secret]")
        for fine in ("Revoke the token: it leaked", "Ask Northwind for the API key rotation date",
                     "Change the database password tonight"):
            self.assertEqual(r(fine), fine)

    def test_followups_keep_their_evidence(self):
        self.write([user(datetime.now().astimezone() - timedelta(hours=2), "draft the vendor message") | {"uuid": "u1"},
                    entry(datetime.now().astimezone() - timedelta(hours=2), "assistant", uuid="u2", message={
                        "id": "m2", "role": "assistant", "model": "claude-test-1",
                        "content": [{"type": "text", "text": "Still open: ask the vendor. Key: sk-ant-abcdefghijklmnopqrstuvwx"}]})])
        todos.merge_followups(self.cfg, {"id": "x", "project": "work/demo"}, [{"text": "old item", "kind": "task"}], [])
        os.environ["FAKE_MODE"] = "quotes"
        followups.scan(self.cfg)
        items = {i["text"]: i for i in todos.load(self.cfg)}
        ev = items["Ask the vendor for the usage breakdown"]["evidence"]
        self.assertEqual((ev["uuid"], ev["role"]), ("u2", "assistant"))
        self.assertEqual(ev["quote"], "Still open: ask the vendor. Key: [secret]")
        self.assertNotIn("evidence", items["Pick a brand"])  # its quote isn't in the transcript
        old = items["old item"]
        self.assertTrue(old["done_at"])
        self.assertEqual(old["closed_evidence"]["uuid"], "u1")

    def test_reopened_item_not_closed_again_by_the_same_session(self):
        todos.merge_followups(self.cfg, {"id": "x", "project": "work/demo"}, [{"text": "old item", "kind": "task"}], [])
        followups.scan(self.cfg)
        old = next(i for i in todos.load(self.cfg) if i["text"] == "old item")
        self.assertEqual(old["closed_by"], SID)
        todos.apply(self.cfg, {"op": "undo", "id": old["id"]})
        old = next(i for i in todos.load(self.cfg) if i["text"] == "old item")
        self.assertEqual((old["done_at"], old["not_closed_by"], "closed_by" in old), (None, [SID], False))
        followups.scan(self.cfg, force=True)
        self.assertIsNone(next(i for i in todos.load(self.cfg) if i["text"] == "old item")["done_at"])
        todos.merge_followups(self.cfg, {"id": "later", "project": "work/demo"}, [], [old["id"]])
        self.assertTrue(next(i for i in todos.load(self.cfg) if i["text"] == "old item")["done_at"])

    def test_copy_in_another_project_is_flagged_then_merged(self):
        add = lambda project, text: todos.merge_followups(self.cfg, {"id": project, "project": project},
                                                          [{"text": text, "kind": "task"}], [])
        add("work/ops", "Run the monthly payroll pre-check before the 25th")
        add("work/payroll", "Run monthly payroll pre-check by the 25th")
        add("work/payroll", "Fix the payroll export")  # too little in common: not flagged
        first, copy, other = todos.load(self.cfg)
        self.assertEqual(copy["possible_duplicate_of"], first["id"])
        self.assertNotIn("possible_duplicate_of", other)
        todos.apply(self.cfg, {"op": "merge", "id": copy["id"]})
        copy = todos.load(self.cfg)[1]
        self.assertEqual((copy["status"], copy["duplicate_of"], "possible_duplicate_of" in copy),
                         ("archived", first["id"], False))
        self.assertEqual(todos.related(self.cfg, "work/payroll"), {"work/ops"})

    def test_not_the_same_is_remembered(self):
        for project, text in (("work/ops", "Renew the TLS certificate for the status page"),
                              ("work/payroll", "Renew the status page TLS certificate")):
            todos.merge_followups(self.cfg, {"id": project, "project": project}, [{"text": text, "kind": "task"}], [])
        first, copy = todos.load(self.cfg)
        todos.apply(self.cfg, {"op": "not_same", "id": copy["id"]})
        items = todos.load(self.cfg)
        self.assertNotIn("possible_duplicate_of", items[1])
        self.assertIsNone(todos.maybe_same(items[1], items, todos.links(self.cfg)["not_same"]))
        self.assertEqual(todos.related(self.cfg, "work/ops"), set())
        self.assertEqual(followups.pairs_across(self.cfg), [])

    def test_related_projects_items_are_shown_and_closed(self):
        todos.merge_followups(self.cfg, {"id": "x", "project": "work/other"}, [{"text": "old item", "kind": "task"}], [])
        followups.scan(self.cfg)
        self.assertNotIn("related projects", self.calls()[-1]["prompt"])
        self.assertIsNone(todos.load(self.cfg)[0]["done_at"])  # another project, not linked: left alone
        todos._link(self.cfg, "related", "work/demo", "work/other")
        followups.scan(self.cfg, force=True)
        self.assertIn("## Open to-dos in related projects", self.calls()[-1]["prompt"])
        self.assertEqual(todos.load(self.cfg)[0]["closed_by"], SID)

    def test_dedupe_across_projects(self):
        for project, text in (("work/ops", "Rotate the backup server SSH key"),
                              ("work/infra", "Rotate the SSH key on the backup server"),
                              ("work/infra", "Order new office chairs")):
            todos.apply(self.cfg, {"op": "add", "text": text, "project": project})
        pairs = followups.pairs_across(self.cfg)
        self.assertEqual([(a["text"], b["text"]) for a, b in pairs],
                         [("Rotate the backup server SSH key", "Rotate the SSH key on the backup server")])
        os.environ["FAKE_MODE"] = "across"  # answers "pair 1 is the same" (and a pair number that doesn't exist)
        with unittest.mock.patch.object(config, "load", return_value=self.cfg), \
             unittest.mock.patch("sys.stdout", io.StringIO()) as out:
            cli.main(["dedupe", "--across", "--apply"])
        self.assertIn("Archived 1 duplicate", out.getvalue())
        self.assertEqual(sum(i.get("status") == "archived" for i in todos.load(self.cfg)), 1)
        self.assertEqual(todos.related(self.cfg, "work/ops"), {"work/infra"})

    def test_dismiss_reasons_steer_later_followups(self):
        followups.scan(self.cfg)
        vendor, brand = todos.load(self.cfg)
        with self.assertRaises(ValueError):
            todos.apply(self.cfg, {"op": "delete", "id": vendor["id"], "reason": "meh"})
        todos.apply(self.cfg, {"op": "delete", "id": vendor["id"], "reason": "not_useful"})
        todos.apply(self.cfg, {"op": "delete", "id": brand["id"]})
        gone = todos.dismissed(self.cfg)
        self.assertEqual([(d["kind"], d.get("reason")) for d in gone], [("external", "not_useful"), ("task", None)])
        followups.scan(self.cfg, force=True)
        prompt = self.calls()[-1]["prompt"]
        noise = prompt.split("## Not useful (don't add things like these)\n")[1]
        self.assertIn(vendor["text"], noise)
        self.assertNotIn(brand["text"], noise)
        stats = todos.kind_stats(self.cfg)
        self.assertEqual((stats["external"]["added"], stats["external"]["not_useful"], stats["task"]["dismissed"]),
                         (1, 1, 1))

    def test_step_waiting_on_an_open_item_goes_in_its_detail(self):
        todos.merge_followups(self.cfg, {"id": "x", "project": "work/demo"},
                              [{"text": "Get the SMTP login from IT", "kind": "waiting", "detail": "Test account only."}], [])
        first = todos.load(self.cfg)[0]
        found, _ = followups.checked({"items": [
            {"text": "Send a test reminder to yourself", "kind": "task", "after": first["id"]},
            {"text": "Renew the domain", "kind": "task", "after": "not-shown"}], "resolved": []}, {first["id"]})
        self.assertNotIn("after", found[1])
        self.assertEqual(todos.merge_followups(self.cfg, {"id": "y", "project": "work/demo"}, found, []), (1, 0))
        self.assertEqual(todos.merge_followups(self.cfg, {"id": "z", "project": "work/demo"}, found[:1], []), (0, 0))
        items = todos.load(self.cfg)
        self.assertEqual(items[0]["detail"], "Test account only. Then: Send a test reminder to yourself")
        self.assertEqual([i["text"] for i in items], ["Get the SMTP login from IT", "Renew the domain"])

    def test_grown_session_sends_only_new_turns_with_the_brief(self):
        old = datetime.now().astimezone() - timedelta(hours=2)
        first = [user(old, "draft the vendor message"), assistant(old, "m1", 5),
                 entry(old, "assistant", message={"id": "m2", "role": "assistant", "model": "claude-test-1",
                       "content": [{"type": "text", "text": "Still open: ask the vendor."}]})]
        followups.scan(self.cfg)
        b = followups.load_briefs(self.cfg)[SID]
        self.assertEqual((b["turns"], b["brief"]["next"]), (3, "send it"))
        self.write(first + [assistant(old, "m3", 5, tool=True)])  # only tool use: nothing new to read
        self.assertEqual(followups.scan(self.cfg)[0], 1)
        self.assertEqual(len(self.calls()), 1)
        self.write(first + [user(old, "the vendor replied, now book the call")])
        followups.scan(self.cfg)
        prompt = self.calls()[-1]["prompt"]
        self.assertIn("## Earlier in this session", prompt)
        self.assertIn('"next": "send it"', prompt)
        self.assertIn("### #4 USER", prompt)
        self.assertNotIn("draft the vendor message", prompt)
        self.assertEqual(followups.load_briefs(self.cfg)[SID]["turns"], 4)
        followups.scan(self.cfg, force=True)  # --again: from the start
        self.assertIn("draft the vendor message", self.calls()[-1]["prompt"])
        self.assertNotIn("## Earlier in this session", self.calls()[-1]["prompt"])

    def test_long_first_read_goes_in_windows(self):
        conv = [{"uuid": str(n), "at": None, "role": "user", "text": f"turn {n} " + "x" * 900} for n in range(40)]
        w = followups.windows(conv, limit=5000)
        self.assertEqual([t["n"] for t in w[0]][:2], [1, 2])
        self.assertTrue(all(sum(len(t["part"]) for t in x) <= 5000 for x in w))
        self.assertEqual(w[-1][-1]["n"], 40)
        capped = followups.windows(conv, limit=5000, most=3)
        self.assertEqual(len(capped), 3)
        self.assertEqual((capped[0][0]["n"], capped[-1][-1]["n"]), (1, 40))
        self.assertEqual([w[0]["n"] for w in followups.windows(conv, limit=5000, most=1)], [capped[-1][0]["n"]])
        self.assertEqual(followups.windows(conv, start=40), [])
        self.assertEqual(followups.windows(conv, start=38)[0][0]["n"], 39)

    def test_checked_keeps_only_shown_ids(self):
        items, resolved = followups.checked({"items": "nonsense", "resolved": ["a", "b", "a", 1]}, {"a"})
        self.assertEqual((items, resolved), ([], [{"id": "a"}]))
        self.assertEqual(followups.checked({}, set()), ([], []))


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import eval_followups  # noqa: E402

FAKE_EVAL_CLAUDE = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": False, "total_cost_usd": 0.001, "duration_ms": 900,
                  "usage": {"input_tokens": 1200, "output_tokens": 80},
                  "structured_output": {"items": [
                      {"text": "Ask Northwind Pay when the API key rotates", "kind": "external"},
                      {"text": "Fix the CSV export header order", "kind": "task"}], "resolved": []}}))
"""


class EvalTests(unittest.TestCase):
    """The follow-up eval's fixtures and scoring (no model calls)."""

    def setUp(self):
        self.cases = eval_followups.load_cases()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rules_match_their_own_labels_only(self):
        self.assertGreaterEqual(len(self.cases), 10)
        for name, spec in self.cases.items():
            exp = spec["expect"]
            for rule in exp["open"]:
                self.assertTrue(eval_followups.matches(rule["label"], rule), (name, rule["label"]))
            for rule in exp.get("ok", []) + exp.get("not_open", []):
                self.assertTrue(eval_followups.matches(rule["label"], rule), (name, rule["label"]))
                self.assertFalse(any(eval_followups.matches(rule["label"], o) for o in exp["open"]),
                                 (name, rule["label"]))

    def test_perfect_answer_scores_perfectly(self):
        for name, spec in self.cases.items():
            added = [{"id": str(n), "text": r["label"]} for n, r in enumerate(spec["expect"]["open"])]
            r = eval_followups.score(spec, added, added)
            self.assertEqual((r["found"], r["hard_found"]), (r["expected"], r["hard_expected"]), name)
            self.assertEqual((r["wrong_open"], r["duplicates"], r["extra"], r["leaks"]), ([], 0, [], []), name)

    def test_wrong_duplicate_and_leaked_items_counted(self):
        spec = self.cases["09-secret-in-text"]
        added = [{"id": "1", "text": "Revoke token ghp_FAKEexample0000 and make a new one"},
                 {"id": "2", "text": "Rotate the GitHub token"},
                 {"id": "3", "text": "Make the script read the token from a file", "evidence": {"quote": "x"}}]
        r = eval_followups.score(spec, added, added)
        self.assertEqual((r["found"], r["duplicates"]), (1, 1))
        self.assertEqual(r["wrong_open"], ["Make the script read the token from a file"])
        self.assertEqual(r["leaks"], ["ghp_FAKEexample"])
        self.assertEqual(r["quoted"], 1)

    def test_near_match_credits_the_item_not_found_yet(self):
        spec = self.cases["12-same-project-rewording"]
        added = [{"id": "1", "text": "Ask Daniel for the SMTP credentials"},
                 {"id": "2", "text": "Test real reminder sending once the SMTP login arrives"}]
        r = eval_followups.score(spec, added, added)
        self.assertEqual((r["found"], r["duplicates"]), (2, 0))
        spec = self.cases["08-closes-earlier"]
        added = [{"id": "1", "text": "Deploy the upload fix tonight", "detail": "then clear the CDN cache"}]
        self.assertEqual(eval_followups.score(spec, added, added)["found"], 1)  # `unless` looks at the text only

    def test_transcripts_read_like_real_ones(self):
        sess, path = eval_followups.build("11", self.cases["11-malformed"], self.root)[0]
        d = followups.digest(path, sess["project"])
        self.assertIn("October statement", d)
        for gone in ("March", "/clear", "24 passed", "broken"):
            self.assertNotIn(gone, d)
        sess, path = eval_followups.build("10", self.cases["10-long-session"], self.root)[0]
        d = followups.digest(path, sess["project"])
        self.assertIn("ingest lag", d)
        self.assertNotIn("quota", d)  # a one-shot digest cuts the middle (how 0.2.0 and earlier lost it)
        read = "\n".join(t["part"] for w in followups.windows(followups.conversation(path)) for t in w)
        self.assertIn("quota", read)

    def test_run_case_with_fake_claude(self):
        fake = self.root / "fake-claude"
        fake.write_text(FAKE_EVAL_CLAUDE)
        fake.chmod(0o755)
        with unittest.mock.patch.object(followups.subprocess, "run", eval_followups._counting_run):
            r = eval_followups.run_case("01", self.cases["01-external-vendor"], "haiku", str(fake))
        self.assertEqual((r["found"], r["expected"]), (1, 2))
        self.assertEqual(r["wrong_open"], ["Fix the CSV export header order"])
        self.assertEqual((r["usage"]["calls"], r["usage"]["input_tokens"]), (1, 1200))
        s = eval_followups.summarise({"01": [dict(r, sessions=1)]})
        self.assertEqual((s["recall"], s["wrong_open"], s["calls"]), (0.5, 1, 1))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


if __name__ == "__main__":
    unittest.main()
