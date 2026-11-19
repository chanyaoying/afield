#!/usr/bin/env python3
"""Unit tests for the contract tests (lib/afield_contracts.py). Run: python3 -I tests/test_contracts.py

Three kinds:
- the pure helpers (flag parsing, file scanning, diffs, when to pause, when to re-run)
- each check against a deliberately broken fake, to prove it notices the change it guards against
- the catalogue stays in step: every contract is documented in CONTRACTS.md and referenced in the code
"""
import glob
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


os.environ["AFIELD_CONFIG"] = os.path.join(tempfile.mkdtemp(), "none.json")  # the defaults, not yours
rc = load("afield_for_contracts", os.path.join(repo, "bin", "afield"))
c = load("afield_contracts", os.path.join(repo, "lib", "afield_contracts.py"))
REGISTRY = {x["id"]: x for x in c.CONTRACTS}


def own_proc_start():
    with open("/proc/self/stat") as fh:
        return fh.read().rsplit(")", 1)[1].split()[19]


class Patched:
    """Temporarily replaces attributes on the afield module (the checks read it as ctx.rc)."""

    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = getattr(rc, key)
            setattr(rc, key, value)
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            setattr(rc, key, value)


def fake_program(text_by_args, extra_bytes=b""):
    """An executable that prints canned --help text; its bytes stand in for the claude binary."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "claude")
    cases = "\n".join(f'  "{args}") cat <<"EOT"\n{text}\nEOT\n  ;;' for args, text in text_by_args.items())
    with open(path, "wb") as fh:
        fh.write(f'#!/bin/sh\ncase "$*" in\n{cases}\nesac\n'.encode() + b"\n# " + extra_bytes + b"\n")
    os.chmod(path, 0o755)
    return path


# --- helpers ----------------------------------------------------------------------------------

class Helpers(unittest.TestCase):
    def test_help_flags(self):
        flags = c.help_flags("  --resume [id]\n  --[no-]chrome   x\n  --session-id <uuid>\n  -p, --print")
        self.assertTrue({"resume", "chrome", "no-chrome", "session-id", "print"} <= flags)

    def test_scan_file_finds_needles_across_chunk_boundaries(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"x" * 13 + b"bridgeSessionId" + b"y" * 40)
        found = c.scan_file(fh.name, ["bridgeSessionId", "procStart"], chunk=16)
        self.assertEqual(found, {"bridgeSessionId"})

    def test_type_problems(self):
        spec = {"pid": int, "cwd": str, "at": (int, float)}
        self.assertEqual(c.type_problems({"pid": 1, "cwd": "/", "at": 1.5}, spec), [])
        problems = c.type_problems({"pid": True, "at": "x"}, spec, "f: ")
        self.assertIn("f: 'pid' is bool, expected int", problems)
        self.assertIn("f: missing key 'cwd'", problems)
        self.assertIn("f: 'at' is str, expected int/float", problems)

    def test_diff_observed_names_what_changed(self):
        old = {"keys": ["a", "b"], "version": "1", "gone": 1}
        new = {"keys": ["b", "c"], "version": "2", "new": [1]}
        lines = c.diff_observed(old, new)
        self.assertIn("keys: +c -a", lines)
        self.assertIn("version: 1 -> 2", lines)
        self.assertIn("gone: no longer seen (was 1)", lines)
        self.assertIn("new: new: [1]", lines)
        self.assertEqual(c.diff_observed(old, old), [])

    def test_snapshot_difference_marks_ok_as_changed(self):
        results = [c.result("SESS-1", c.OK, [], {"keys": ["pid", "remoteId"]}),
                   c.result("SESS-2", c.FAIL, ["x"], {"keys": []}),
                   c.result("CLI-1", c.OK, [], {"flags": ["resume"]})]
        snap = {"observed": {"SESS-1": {"keys": ["pid", "bridgeSessionId"]}, "SESS-2": {"keys": ["y"]},
                             "CLI-1": {"flags": ["resume"]}}}
        c.compare_to_snapshot(results, snap)
        self.assertEqual(results[0]["status"], c.CHANGED)
        self.assertIn("keys: +remoteId -bridgeSessionId", results[0]["detail"])
        self.assertEqual(results[1]["status"], c.FAIL)  # a failure stays a failure
        self.assertEqual(results[2]["status"], c.OK)

    def test_only_critical_failures_pause(self):
        registry = {"A": {"critical": True}, "B": {"critical": False}, "C": {"critical": True}}
        results = [c.result("A", c.FAIL), c.result("B", c.FAIL), c.result("C", c.CHANGED)]
        self.assertEqual(c.blocking(results, registry), ["A"])
        self.assertEqual(c.blocking([c.result("A", c.SKIP)], registry), [])
        manual = {"E": {"critical": True, "level": "e2e"}}
        self.assertEqual(c.blocking([c.result("E", c.FAIL)], manual), [])  # e2e is manual: it never pauses

    def test_when_the_supervisor_runs_the_checks_again(self):
        saved = {"fingerprint": "v1", "ran_at": 1000, "results": [c.result("X", c.OK)]}
        self.assertTrue(c.is_stale(None, "v1", 1000))
        self.assertTrue(c.is_stale(saved, "v2", 1001))  # a new version: at once
        self.assertFalse(c.is_stale(saved, "v1", 1000 + 3600))
        self.assertTrue(c.is_stale(saved, "v1", 1000 + 6 * 3600))
        with_skip = dict(saved, results=[c.result("X", c.SKIP)])
        self.assertFalse(c.is_stale(with_skip, "v1", 1000 + 60))
        self.assertTrue(c.is_stale(with_skip, "v1", 1000 + 600))
        turned_off = dict(saved, results=[c.turned_off("X", "archive_sync")])
        self.assertFalse(c.is_stale(turned_off, "v1", 1000 + 600))  # off in the config: not retried

    def test_report_points_at_the_catalogue(self):
        results = [c.result("SESS-1", c.FAIL, ["1.json: missing key 'procStart'"]), c.result("CLI-1", c.OK)]
        text = c.format_report(results, {"claude": "9.9.9"})
        self.assertIn("FAIL     SESS-1", text)
        self.assertIn("missing key 'procStart'", text)
        self.assertIn("see CONTRACTS.md#sess-1 · used by live_processes", text)
        self.assertIn("paused until fixed", text)
        self.assertIn("1 ok, 0 changed, 1 failed, 0 skipped", text)

    def test_status_line(self):
        self.assertIn("not checked yet", rc.contracts_line(None, 0))
        ok = {"ran_at": 0, "versions": {"claude": "2.1.319"}, "blocking": [],
              "results": [{"id": "A", "status": "ok"}, {"id": "B", "status": "skip"}]}
        self.assertEqual(rc.contracts_line(ok, 120), "ok (1 pass, 1 skip) · claude 2.1.319 · checked 2 min ago")
        bad = dict(ok, blocking=["SESS-1"])
        self.assertTrue(rc.contracts_line(bad, 0).startswith("FAILED SESS-1"))


# --- each check notices the change it guards against ------------------------------------------

class StaticChecks(unittest.TestCase):
    HELP = "--resume --session-id --remote-control --print"
    SERVER = "--[no-]create-session-in-dir --spawn"

    def ctx(self, help_text=HELP, server=SERVER, extra=b""):
        return c.Ctx(rc, fake_program({"--help": help_text, "remote-control --help": server}, extra))

    def test_flags_present(self):
        self.assertEqual(c.check_cli_flags(self.ctx())["status"], c.OK)
        self.assertEqual(c.check_server_flags(self.ctx())["status"], c.OK)

    def test_a_removed_flag_fails_and_is_named(self):
        r = c.check_cli_flags(self.ctx(help_text="--resume --session-id --rc"))
        self.assertEqual(r["status"], c.FAIL)
        self.assertEqual(r["detail"], ["`claude --help` no longer lists --remote-control"])
        r = c.check_server_flags(self.ctx(server="--spawn"))
        self.assertEqual(r["status"], c.FAIL)

    def test_a_renamed_field_in_the_binary_fails(self):
        names = [n for n in c.BINARY_NAMES if n != "procStart"]
        r = c.check_binary_names(self.ctx(extra=" ".join(names).encode()))
        self.assertEqual(r["status"], c.FAIL)
        self.assertTrue(any("'procStart' no longer appears" in d for d in r["detail"]))

    def test_even_missing_while_its_unit_is_enabled_fails(self):
        # 2026-11-10: doctor said "skip: not installed" while Even crash-looped on a PATH without it.
        ctx = self.ctx()
        ctx.even = None
        for wanted, status in ((True, c.FAIL), (False, c.SKIP)):
            with Patched(even_expected=lambda props=None, w=wanted: w):
                for check in (c.check_even_flags, c.check_even_port_env):
                    r = check(ctx)
                    self.assertEqual(r["status"], status, (check.__name__, wanted))
            if wanted:
                self.assertIn("not on afield's PATH", r["detail"][0])
                self.assertEqual(c.blocking([r]), [])  # sessions still run, just without the glasses

    def test_screen_text(self):
        good = self.ctx(extra=" ".join(c.SCREEN_TEXT).encode())
        self.assertEqual(c.check_screen_text(good)["status"], c.OK)
        reworded = [t for t in c.SCREEN_TEXT if t != "standing down"] + ["stood down"]
        r = c.check_screen_text(self.ctx(extra=" ".join(reworded).encode()))
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("'standing down'", r["detail"][0])

    def test_env_setup_text(self):
        good = self.ctx(extra=" ".join(c.ENV_SETUP_TEXT).encode())
        self.assertEqual(c.check_env_setup_text(good)["status"], c.OK)
        # Claude Code reorders the options: "Don't show again" is no longer two Downs from "Yes".
        reordered = [t for t in c.ENV_SETUP_TEXT if not t.startswith("[")] + [
            '[{label:"Yes",value:"accept"},{label:"Don\'t show again",value:"dismiss"},'
            '{label:"Not now",value:"later"}]']
        r = c.check_env_setup_text(self.ctx(extra=" ".join(reordered).encode()))
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("Not now", r["detail"][0])
        self.assertEqual(c.blocking([r]), [])  # reported, but nothing else pauses


class CredentialsOnlyWhenUsed(unittest.TestCase):
    def test_token_file_is_not_read_while_nothing_uses_it(self):
        with Patched(ARCHIVE_SYNC=False, PHONE_TITLES=False, RETIRE_ARCHIVES=False,
                     CREDENTIALS="/nonexistent/should-not-be-opened"):
            r = c.check_credentials(c.Ctx(rc))
        self.assertEqual((r["status"], r.get("off")), (c.SKIP, True))
        with Patched(ARCHIVE_SYNC=False, PHONE_TITLES=False, RETIRE_ARCHIVES=True,
                     CREDENTIALS="/nonexistent/x"):
            self.assertEqual(c.check_credentials(c.Ctx(rc))["status"], c.FAIL)


class SessionChecks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.good = {"pid": os.getpid(), "procStart": own_proc_start(), "sessionId": "u1", "cwd": "/x",
                     "status": "idle", "statusUpdatedAt": 1, "bridgeSessionId": "session_abc"}

    def write(self, record):
        with open(os.path.join(self.dir, f"{os.getpid()}.json"), "w") as fh:
            json.dump(record, fh)

    def run_check(self, fn, record):
        self.write(record)
        with Patched(SESSIONS_DIR=self.dir):
            return fn(c.Ctx(rc))

    def test_good_session_file(self):
        self.assertEqual(self.run_check(c.check_session_files, self.good)["status"], c.OK)
        self.assertEqual(self.run_check(c.check_proc_start, self.good)["status"], c.OK)

    def test_renamed_key_fails(self):
        record = dict(self.good)
        record["processStart"] = record.pop("procStart")
        r = self.run_check(c.check_session_files, record)
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn(f"{os.getpid()}.json: missing key 'procStart'", r["detail"])
        self.assertIn("processStart", r["observed"]["keys"])  # the diff will show the new name

    def test_missing_link_fails(self):
        record = {k: v for k, v in self.good.items() if k != "bridgeSessionId"}
        r = self.run_check(c.check_session_files, record)
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("no live session records a bridgeSessionId (its phone link)", r["detail"])

    def test_phone_link_is_not_in_the_compared_keys(self):
        # A session without a link (a glasses turn) must not make the snapshot diff flicker.
        r = self.run_check(c.check_session_files, self.good)
        self.assertNotIn("bridgeSessionId", r["observed"]["keys"])

    def test_new_status_value_is_reported(self):
        r = self.run_check(c.check_session_files, dict(self.good, status="thinking"))
        self.assertEqual(r["status"], c.CHANGED)
        self.assertIn("new status value 'thinking'", r["detail"][0])

    def test_wrong_proc_start_fails(self):
        r = self.run_check(c.check_proc_start, dict(self.good, procStart="1"))
        self.assertEqual(r["status"], c.FAIL)

    def test_no_sessions_is_a_skip(self):
        with Patched(SESSIONS_DIR=tempfile.mkdtemp()):
            self.assertEqual(c.check_session_files(c.Ctx(rc))["status"], c.SKIP)


class TranscriptChecks(unittest.TestCase):
    def setUp(self):
        self.sessions, self.projects = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.uuid, self.cwd = "11111111-2222-3333-4444-555555555555", "/home/me/afield"
        with open(os.path.join(self.sessions, f"{os.getpid()}.json"), "w") as fh:
            json.dump({"pid": os.getpid(), "procStart": own_proc_start(), "sessionId": self.uuid, "cwd": self.cwd,
                       "status": "idle", "statusUpdatedAt": 1, "bridgeSessionId": "session_XYZ"}, fh)
        self.registry = {self.uuid: {"uuid": self.uuid, "state": "open", "link": "session_XYZ", "cwd": self.cwd}}

    def transcript(self, *lines):
        with Patched(PROJECTS_DIR=self.projects):
            path = rc.transcript_path(self.cwd, self.uuid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def check(self, fn):
        with Patched(SESSIONS_DIR=self.sessions, PROJECTS_DIR=self.projects, load=lambda: self.registry):
            return fn(c.Ctx(rc))

    def test_path_encoding(self):
        self.transcript('{"type":"user"}')
        self.assertTrue(os.path.isdir(os.path.join(self.projects, "-home-me-afield")))
        self.assertEqual(self.check(c.check_transcript_path)["status"], c.OK)

    def test_matching_bridge_line(self):
        self.transcript('{"type":"bridge-session","sessionId":"%s","bridgeSessionId":"cse_XYZ"}' % self.uuid)
        self.assertEqual(self.check(c.check_bridge_line)["status"], c.OK)

    def test_bridge_line_naming_another_link_fails(self):
        self.transcript('{"type":"bridge-session","sessionId":"%s","bridgeSessionId":"cse_OTHER"}' % self.uuid)
        r = self.check(c.check_bridge_line)
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("expected 'cse_XYZ'", r["detail"][0])

    def test_renamed_bridge_record_type_fails(self):
        self.transcript('{"type":"remote-link","sessionId":"%s","bridgeSessionId":"cse_XYZ"}' % self.uuid)
        r = self.check(c.check_bridge_line)
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("transcript has no bridge-session line", r["detail"][0])

    def test_phone_chat_still_on_the_server_is_not_checked(self):
        # The server writes no bridge-session line; only a window's --remote-control does.
        self.registry[self.uuid]["host"] = "server"
        self.transcript('{"type":"user"}')
        self.assertEqual(self.check(c.check_bridge_line)["status"], c.SKIP)

    def test_compact_json(self):
        self.transcript('{"type":"user","message":{"content":"hi"}}')
        self.assertEqual(self.check(c.check_compact_json)["status"], c.OK)
        self.transcript('{"type": "user", "message": {"content": "hi"}}')
        self.assertEqual(self.check(c.check_compact_json)["status"], c.FAIL)


class EvenGlassesChecks(unittest.TestCase):
    """EVEN-4/EVEN-5: what afield even-proxy relies on."""

    def package(self, source):
        pkg = tempfile.mkdtemp()
        os.makedirs(os.path.join(pkg, "bin"))
        with open(os.path.join(pkg, "package.json"), "w") as fh:
            fh.write("{}")
        exe = os.path.join(pkg, "bin", "cli.js")
        with open(exe, "w") as fh:
            fh.write(source)
        return exe

    def test_host_env_and_hook_route_found(self):
        ctx = c.Ctx(rc)
        ctx.even = self.package("process.env.EVEN_HOST_MODE; process.env.EVEN_HOST_INTERFACE; "
                                'app.post("/api/claude-sync/hook")')
        with Patched(even_network=lambda: {"mode": "tailscale"}):
            self.assertEqual(c.check_even_host_env(ctx)["status"], c.OK)

    def test_a_moved_hook_route_fails(self):
        # The proxy blocks the loopback-trusted route by name; a rename must not slip through.
        ctx = c.Ctx(rc)
        ctx.even = self.package('process.env.EVEN_HOST_MODE; process.env.EVEN_HOST_INTERFACE; app.post("/api/hook")')
        with Patched(even_network=lambda: {"mode": "tailscale"}):
            r = c.check_even_host_env(ctx)
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("/api/claude-sync/hook", r["detail"][0])
        self.assertEqual(c.blocking([r]), [])  # the glasses list everything again; nothing else stops

    def test_an_unsupported_network_mode_fails(self):
        ctx = c.Ctx(rc)
        ctx.even = self.package("EVEN_HOST_MODE EVEN_HOST_INTERFACE /api/claude-sync/hook")
        with Patched(even_network=lambda: {"mode": "expose", "provider": "ngrok"}):
            self.assertEqual(c.check_even_host_env(ctx)["status"], c.FAIL)

    def serve(self, body):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_a):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".even-terminal"))
        with open(os.path.join(home, ".even-terminal", "config.json"), "w") as fh:
            json.dump({"token": "t"}, fh)
        return Patched(EVEN_PORT=server.server_address[1], HOME=home, even_up=lambda: True)

    def test_session_list_shape(self):
        with self.serve({"sessions": [{"id": "u1", "title": "x", "cwd": "/"}]}):
            self.assertEqual(c.check_even_sessions(c.Ctx(rc))["status"], c.OK)

    def test_a_renamed_title_field_fails(self):
        with self.serve({"sessions": [{"id": "u1", "name": "x"}]}):
            r = c.check_even_sessions(c.Ctx(rc))
        self.assertEqual(r["status"], c.FAIL)
        self.assertTrue(any("title" in d for d in r["detail"]))


class ApiChecks(unittest.TestCase):
    ITEM = {"id": "session_1", "session_status": "idle", "updated_at": "2026-11-09T00:00:00Z", "title": "t"}

    def check(self, response):
        def fake(path, data=None, timeout=10):
            if isinstance(response, Exception):
                raise response
            return response
        with Patched(api_request=fake, ARCHIVE_SYNC=True):
            return c.check_api_list(c.Ctx(rc))

    def test_not_called_when_its_feature_is_off(self):
        def fake(*_a, **_k):
            raise AssertionError("the API was called")
        with Patched(api_request=fake, ARCHIVE_SYNC=False, PHONE_TITLES=False):
            for check in (c.check_api_list, c.check_api_one):
                r = check(c.Ctx(rc))
                self.assertEqual((r["status"], r.get("off")), (c.SKIP, True))

    def test_good_list(self):
        self.assertEqual(self.check({"data": [self.ITEM], "has_more": False})["status"], c.OK)

    def test_renamed_field_fails(self):
        item = {k: v for k, v in self.ITEM.items() if k != "session_status"}
        item["status"] = "idle"
        r = self.check({"data": [item]})
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("session_1: missing key 'session_status'", r["detail"])

    def test_http_error_fails_but_offline_is_a_skip(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.assertEqual(self.check(err)["detail"], ["HTTP 404 Not Found"])
        self.assertEqual(self.check(err)["status"], c.FAIL)
        self.assertEqual(self.check(urllib.error.URLError("down"))["status"], c.SKIP)

    def test_optional_fields_do_not_count_as_a_change(self):
        r = self.check({"data": [dict(self.ITEM, post_turn_summary="x"), self.ITEM]})
        self.assertEqual(r["observed"]["item"], sorted(self.ITEM))

    def test_new_status_value_is_reported(self):
        r = self.check({"data": [dict(self.ITEM, session_status="paused")]})
        self.assertEqual(r["status"], c.CHANGED)

    def test_a_crashing_check_counts_as_a_failure(self):
        def boom(_ctx):
            raise KeyError("data")
        saved = list(c.CONTRACTS)
        c.CONTRACTS[:] = [dict(id="X-1", level="live", title="t", used_by=[], critical=True, fn=boom)]
        try:
            with Patched(SESSIONS_DIR=tempfile.mkdtemp()):
                results = [r for r in c.run(rc, ("live",))[0]]
        finally:
            c.CONTRACTS[:] = saved
        self.assertEqual(results[0]["status"], c.FAIL)
        self.assertIn("the check crashed: KeyError", results[0]["detail"][0])


class SystemdCheck(unittest.TestCase):
    FILES = {"memory.current": "100\n", "memory.stat": "anon 50\ninactive_file 30\n", "memory.events": "oom_kill 0\n"}
    SHOWN = {"afield.service": {"ActiveState": "active", "InvocationID": "a", "NRestarts": "0"},
             "afield-tmux.service": {"ActiveState": "active", "InvocationID": "b"}}

    def check(self, files=None, shown=None):
        files = dict(self.FILES, **(files or {}))
        with Patched(slice_file=lambda name: files.get(name, ""), units_show=lambda u, p: shown or self.SHOWN,
                     SERVER_UNIT="afield.service", TMUX_UNIT="afield-tmux.service"):
            return c.check_systemd(c.Ctx(rc))

    def test_good(self):
        self.assertEqual(self.check()["status"], c.OK)

    def test_missing_inactive_file_fails(self):
        r = self.check({"memory.stat": "anon 50\n"})
        self.assertEqual(r["status"], c.FAIL)
        self.assertIn("inactive_file", r["detail"][0])

    def test_missing_restart_count_fails(self):
        shown = {k: dict(v) for k, v in self.SHOWN.items()}
        del shown["afield.service"]["NRestarts"]
        self.assertEqual(self.check(shown=shown)["status"], c.FAIL)


class Pause(unittest.TestCase):
    def test_keep_link_writes_nothing_while_a_contract_fails(self):
        state, projects = os.path.join(tempfile.mkdtemp(), "contracts.json"), tempfile.mkdtemp()
        entry = {"uuid": "u-1", "cwd": "/x", "link": "session_A"}
        with Patched(CONTRACTS_STATE=state, PROJECTS_DIR=projects, log=lambda message: None):
            path = rc.transcript_path("/x", "u-1")
            os.makedirs(os.path.dirname(path))
            open(path, "w").close()
            with open(state, "w") as fh:
                json.dump({"blocking": ["TRANS-2"]}, fh)
            self.assertEqual(rc.contracts_paused(), ["TRANS-2"])
            rc.keep_link(entry)
            with open(path) as fh:
                self.assertEqual(fh.read(), "")
            with open(state, "w") as fh:
                json.dump({"blocking": []}, fh)
            rc.keep_link(entry)
            with open(path) as fh:
                self.assertIn('"bridgeSessionId":"cse_A"', fh.read())

    def test_no_saved_result_means_no_pause(self):
        with Patched(CONTRACTS_STATE=os.path.join(tempfile.mkdtemp(), "none.json")):
            self.assertEqual(rc.contracts_paused(), [])


# --- the catalogue stays in step with the code -------------------------------------------------

class Catalogue(unittest.TestCase):
    def test_ids_are_unique_and_levels_known(self):
        ids = [x["id"] for x in c.CONTRACTS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(x["level"] in c.LEVELS for x in c.CONTRACTS))
        self.assertTrue(all(x["fn"] or x["level"] == "e2e" for x in c.CONTRACTS))

    def test_used_by_names_exist(self):
        units = set(os.listdir(os.path.join(repo, "systemd")))
        for x in c.CONTRACTS:
            for name in x["used_by"]:
                self.assertTrue(callable(getattr(rc, name, None)) or name in units,
                                f"{x['id']} says it is used by {name}, which does not exist")

    def test_every_contract_is_documented(self):
        with open(os.path.join(repo, "CONTRACTS.md")) as fh:
            doc = fh.read()
        headings = set(re.findall(r"^### ([A-Z0-9]+-\d+)\b", doc, re.M))
        self.assertEqual(headings, set(REGISTRY), "CONTRACTS.md headings and the registry differ")

    def test_code_references_are_real_and_complete(self):
        refs = set()
        for path in [os.path.join(repo, "bin", "afield")] + glob.glob(os.path.join(repo, "systemd", "*")):
            with open(path) as fh:
                for group in re.findall(r"contract: ((?:[A-Z0-9]+-\d+(?:, )?)+)", fh.read()):
                    refs |= set(group.split(", "))
        self.assertEqual(refs - set(REGISTRY), set(), "code names a contract that does not exist")
        self.assertEqual(set(REGISTRY) - refs, set(), "a contract is not marked where the code relies on it")

    def test_snapshot_only_names_known_contracts(self):
        snap = c.load_snapshot()
        self.assertIsNotNone(snap, "contracts/snapshot.json is missing: run afield doctor --update-snapshot")
        self.assertEqual(set(snap["observed"]) - set(REGISTRY), set())


if __name__ == "__main__":
    unittest.main()
