#!/usr/bin/env python3
"""Decision tests for the afield supervisor. Run: python3 -I test_afield.py"""
import importlib.machinery
import importlib.util
import json
import subprocess
import os
import tempfile
import unittest

os.environ["AFIELD_CONFIG"] = os.path.join(tempfile.mkdtemp(), "none.json")  # the defaults, not yours

here = os.path.dirname(os.path.abspath(__file__))
path = os.environ.get("AFIELD_SCRIPT") or next(
    (p for p in (os.path.join(here, "..", "bin", "afield"), os.path.expanduser("~/.local/bin/afield"))
     if os.path.exists(p)))
loader = importlib.machinery.SourceFileLoader("afield", path)
spec = importlib.util.spec_from_loader("afield", loader)
rc = importlib.util.module_from_spec(spec)
loader.exec_module(rc)


def entry(**kw):
    base = dict(uuid="u1", host="tmux", invocation="t1", state="open", absent=0)
    base.update(kw)
    return base


INV = {"server": "s1", "tmux": "t1"}
NOW = 10_000


class Decide(unittest.TestCase):
    def d(self, e, live=(), inv=INV, oom=False):
        return rc.decide(e, set(live), inv, oom, NOW)

    def test_live_session_needs_nothing(self):
        self.assertEqual(self.d(entry(), live={"u1"}), "none")

    def test_closed_session_is_never_reopened(self):
        self.assertEqual(self.d(entry(state="closed")), "none")

    def test_failed_session_is_not_retried(self):
        self.assertEqual(self.d(entry(state="failed")), "none")

    def test_host_down_waits(self):
        self.assertEqual(self.d(entry(), inv={"server": "s1", "tmux": None}), "none")

    def test_tmux_restarted_reopens(self):
        # A reboot or tmux restart takes every window with it; nobody closed anything.
        self.assertEqual(self.d(entry(), inv={"server": "s1", "tmux": "t2"}), "reopen")

    def test_first_absence_with_same_host_only_counts(self):
        self.assertEqual(self.d(entry(absent=0)), "count")

    def test_second_absence_with_same_host_means_user_closed_it(self):
        # You typed /exit in the window.
        self.assertEqual(self.d(entry(absent=1)), "closed")

    def test_oom_killed_session_is_reopened_not_closed(self):
        self.assertEqual(self.d(entry(absent=1), oom=True), "reopen")

    def test_session_that_died_right_after_launch_is_retried_not_closed(self):
        self.assertEqual(self.d(entry(absent=1, launched_at=NOW - 20)), "reopen")

    def test_old_background_session_is_moved_to_tmux(self):
        self.assertEqual(self.d(entry(host="daemon", invocation="d0")), "reopen")

    def test_even_restart_reopens_wrapped_sessions(self):
        # The wrapper exits with its Claude when the Even server goes away; nobody closed it.
        e = entry(absent=1, wrapped=True, even="e1")
        self.assertEqual(self.d(e, inv={**INV, "even": "e2"}), "reopen")

    def test_wrapped_session_waits_while_even_is_down(self):
        e = entry(absent=1, wrapped=True, even="e1")
        self.assertEqual(self.d(e, inv={**INV, "even": None}), "none")

    def test_wrapped_session_stops_waiting_once_the_even_wait_runs_out(self):
        # 2026-11-10: Even crash-looped (binary not on its PATH) and held every glasses session for
        # 25 min. Past the wait, they start without it and go back to it once it is up.
        e = entry(absent=1, wrapped=True, even="e1")
        self.assertEqual(self.d(e, inv={**INV, "even": None, "even_waiting": False}), "reopen")
        self.assertEqual(self.d(e, inv={**INV, "even": None, "even_waiting": True}), "none")

    def test_same_even_and_gone_twice_means_closed(self):
        e = entry(absent=1, wrapped=True, even="e1")
        self.assertEqual(self.d(e, inv={**INV, "even": "e1"}), "closed")

    def test_phone_child_waits_for_server(self):
        e = entry(host="server", invocation="s1", absent=0)
        self.assertEqual(self.d(e, inv={"server": None, "tmux": "t1"}), "none")


class LaunchCommand(unittest.TestCase):
    def test_wrapped_so_the_glasses_can_join(self):
        self.assertEqual(
            rc.launch_command("abc-uuid", wrapped=True),
            ["even-terminal", "claude", "--resume", "abc-uuid", "--remote-control"],
        )

    def test_plain_when_even_terminal_is_unavailable(self):
        self.assertEqual(
            rc.launch_command("abc-uuid", wrapped=False),
            ["claude", "--resume", "abc-uuid", "--remote-control"],
        )

    def test_no_name_flag_so_the_phone_title_is_never_overwritten(self):
        self.assertNotIn("--name", rc.launch_command("abc-uuid", wrapped=True))

    def test_new_session_gets_its_id_and_phone_title(self):
        self.assertEqual(
            rc.launch_command("abc-uuid", wrapped=True, new=True, title="Groceries"),
            ["even-terminal", "claude", "--session-id", "abc-uuid", "--remote-control", "Groceries"],
        )

    def test_new_session_without_a_title(self):
        self.assertEqual(rc.launch_command("abc-uuid", wrapped=False, new=True),
                         ["claude", "--session-id", "abc-uuid", "--remote-control"])


class LinkRecord(unittest.TestCase):
    def test_phone_link_becomes_bridge_session_record(self):
        self.assertEqual(
            rc.link_record("session_015ABCxyz", "abc-uuid"),
            {"type": "bridge-session", "bridgeSessionId": "cse_015ABCxyz", "sessionId": "abc-uuid"},
        )

    def test_missing_or_odd_link_gives_no_record(self):
        self.assertIsNone(rc.link_record(None, "abc-uuid"))
        self.assertIsNone(rc.link_record("env_015ABC", "abc-uuid"))


class Convert(unittest.TestCase):
    def proc(self, **kw):
        base = dict(host="server", status="idle", status_at=0, cwd="/srv", has_chat=True)
        base.update(kw)
        return base

    def test_idle_phone_session_with_a_chat_is_converted(self):
        self.assertTrue(rc.should_convert(self.proc(), now_ms=60_000, min_idle_ms=20_000))

    def test_idle_old_background_session_is_converted(self):
        self.assertTrue(rc.should_convert(self.proc(host="daemon"), 60_000, 20_000))

    def test_busy_session_waits(self):
        self.assertFalse(rc.should_convert(self.proc(status="busy"), 60_000, 20_000))

    def test_just_finished_session_waits_in_case_you_are_still_typing(self):
        self.assertFalse(rc.should_convert(self.proc(status_at=50_000), 60_000, 20_000))

    def test_empty_session_is_left_alone(self):
        self.assertFalse(rc.should_convert(self.proc(has_chat=False), 60_000, 20_000))

    def test_session_in_its_window_is_already_converted(self):
        self.assertFalse(rc.should_convert(self.proc(host="tmux"), 60_000, 20_000))

    def test_terminal_session_you_handed_over_is_moved_once_idle(self):
        self.assertTrue(rc.should_convert(self.proc(host="external"), 60_000, 20_000, handover=True))

    def test_terminal_session_you_did_not_hand_over_is_never_touched(self):
        self.assertFalse(rc.should_convert(self.proc(host="external"), 60_000, 20_000))

    def test_handed_over_session_waits_while_busy(self):
        self.assertFalse(rc.should_convert(self.proc(host="external", status="busy"), 60_000, 20_000, handover=True))

    def test_idle_wait_is_five_seconds_and_a_tick_is_no_longer(self):
        self.assertEqual(rc.IDLE_MS, 5_000)
        self.assertLessEqual(rc.TICK_SECONDS, 5)


class Adopt(unittest.TestCase):
    def t(self, **kw):
        base = dict(uuid="g1", entrypoint="sdk-ts", created=500, mtime=900)
        base.update(kw)
        return base

    def a(self, t, tracked=(), live=()):
        return rc.should_adopt(t, now=1000, since=100, tracked=set(tracked), live=set(live), min_idle=20)

    def test_idle_glasses_session_is_adopted(self):
        self.assertTrue(self.a(self.t()))

    def test_glasses_session_mid_turn_waits(self):
        self.assertFalse(self.a(self.t(mtime=990)))

    def test_running_glasses_session_waits(self):
        self.assertFalse(self.a(self.t(), live={"g1"}))

    def test_already_tracked_session_is_not_adopted_again(self):
        self.assertFalse(self.a(self.t(), tracked={"g1"}))

    def test_glasses_sessions_from_before_adoption_was_switched_on_are_left_alone(self):
        self.assertFalse(self.a(self.t(created=50)))

    def test_terminal_and_phone_sessions_are_not_glasses_sessions(self):
        self.assertFalse(self.a(self.t(entrypoint="cli")))
        self.assertFalse(self.a(self.t(entrypoint="sdk-cli")))


class Trust(unittest.TestCase):
    PROMPT = """
 Accessing workspace:

 /home/alice

 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
   Yes, I trust this folder
"""

    def test_prompt_folder_is_read(self):
        self.assertEqual(rc.trust_prompt_dir(self.PROMPT), "/home/alice")

    def test_normal_screen_is_not_a_prompt(self):
        self.assertIsNone(rc.trust_prompt_dir("❯ Reply with only the word: one\n● one\n"))

    def test_no_folder_is_answered_unless_configured(self):
        self.assertEqual(rc.AUTO_TRUST_DIRS, [])


class Host(unittest.TestCase):
    PANES = {100: "u1", 200: ""}

    def test_process_under_a_tagged_window_is_ours(self):
        self.assertEqual(rc.classify([300, 150, 100, 1], self.PANES, ""), "tmux")

    def test_process_in_your_own_window_is_external(self):
        self.assertEqual(rc.classify([300, 200, 1], self.PANES, ""), "external")

    def test_server_child_by_cgroup(self):
        self.assertEqual(rc.classify([300, 1], self.PANES, "0::/x/afield.service"), "server")

    def test_old_background_session_by_cgroup(self):
        # a Claude Code background session (claude daemon), e.g. a chat branched with a slash command
        self.assertEqual(rc.classify([300, 1], self.PANES, "0::/x/tmux-spawn-1.scope", "bg"), "daemon")
        self.assertEqual(rc.classify([300, 1], self.PANES, "0::/x/tmux-spawn-1.scope", "interactive"), "external")


class Windows(unittest.TestCase):
    def test_live_window_is_left_alone(self):
        self.assertEqual(rc.windows_to_close([("@1", "u1", False)]), [])

    def test_dead_window_is_closed(self):
        self.assertEqual(rc.windows_to_close([("@1", "u1", True)]), ["@1"])

    def test_your_own_windows_are_never_touched(self):
        self.assertEqual(rc.windows_to_close([("@1", "", True), ("@2", "", False)]), [])

    def test_duplicate_windows_for_one_session_collapse_to_one(self):
        self.assertEqual(rc.windows_to_close([("@1", "u1", False), ("@2", "u1", False)]), ["@2"])

    def test_window_name_is_the_phone_title(self):
        self.assertEqual(rc.window_name({"title": "Ping ping", "name": "afield-52", "uuid": "aa32ff14-x"}),
                         "Ping ping")

    def test_window_name_falls_back_to_local_name(self):
        self.assertEqual(rc.window_name({"name": "afield-52", "uuid": "aa32ff14-x"}), "afield-52")

    def test_long_titles_are_shortened(self):
        self.assertEqual(len(rc.window_name({"title": "x" * 80, "uuid": "u"})), 30)


class Retire(unittest.TestCase):
    PROCS = [dict(pid=100, uuid="u1"), dict(pid=200, uuid="u2")]

    def test_session_is_found_from_the_process_that_asked(self):
        self.assertEqual(rc.session_of([555, 100, 1], self.PROCS), "u1")

    def test_no_session_when_run_outside_one(self):
        self.assertIsNone(rc.session_of([555, 1], self.PROCS))

    def test_archive_url_uses_the_cse_form_of_the_link(self):
        self.assertEqual(rc.archive_path("session_015ABC"), "/v1/code/sessions/cse_015ABC/archive")


class ArchivedOnPhone(unittest.TestCase):
    """statuses: {link: (status, updated_at)}."""

    def e(self, **kw):
        base = dict(uuid="u1", state="open", link="session_A", launched_at=0)
        base.update(kw)
        return base

    def r(self, entries, statuses, live=("u1", "u2")):
        return rc.archived_to_retire(entries, statuses, set(live), now=1000, grace=120)

    def test_running_session_archived_in_the_app_is_retired(self):
        self.assertEqual(self.r([self.e()], {"session_A": ("archived", 900)}), ["u1"])

    def test_session_that_archived_itself_at_shutdown_is_kept(self):
        # Every chat archives itself when the machine shuts down (reboot 2026-11-08).
        self.assertEqual(self.r([self.e()], {"session_A": ("archived", 510)}, live=()), [])

    def test_message_sent_while_the_machine_was_down_does_not_retire_it(self):
        # updated_at moves when you send a message, not when anything is archived.
        self.assertEqual(self.r([self.e()], {"session_A": ("archived", 990)}, live=()), [])

    def test_active_session_is_kept(self):
        self.assertEqual(self.r([self.e()], {"session_A": ("idle", 900)}), [])

    def test_session_missing_from_the_list_is_kept(self):
        self.assertEqual(self.r([self.e()], {}), [])

    def test_just_launched_session_is_kept_while_its_status_catches_up(self):
        self.assertEqual(self.r([self.e(launched_at=950)], {"session_A": ("archived", 800)}), [])

    def test_forgotten_and_linkless_entries_are_ignored(self):
        entries = [self.e(state="forgotten"), self.e(uuid="u2", link=None)]
        self.assertEqual(self.r(entries, {"session_A": ("archived", 900)}), [])


class Scope(unittest.TestCase):
    def test_only_the_served_folder_is_tracked(self):
        self.assertTrue(rc.in_scope("/home/u/afield", "/home/u/afield"))
        self.assertFalse(rc.in_scope("/home/u/afield-proto/d", "/home/u/afield"))
        self.assertFalse(rc.in_scope(None, "/home/u/afield"))


class Duplicates(unittest.TestCase):
    """A server child for a windowed session means the phone reached the server, not the window
    (2026-11-09: the window's Remote Control stood down with code 4090 and every phone message
    started a server child, which the old rule killed within 15s, mid-reply)."""

    def p(self, **kw):
        base = dict(uuid="u1", host="server", pid=10, status="idle", status_at=0)
        base.update(kw)
        return base

    def d(self, procs):
        return rc.duplicates_to_stop(procs, now_ms=60_000, min_idle_ms=10_000)

    def test_idle_server_child_is_stopped_when_window_copy_exists(self):
        self.assertEqual(self.d([self.p(), self.p(host="tmux", pid=20)]), [10])

    def test_busy_server_child_is_left_to_finish_your_reply(self):
        self.assertEqual(self.d([self.p(status="busy"), self.p(host="tmux", pid=20)]), [])

    def test_just_finished_server_child_waits_in_case_you_reply(self):
        self.assertEqual(self.d([self.p(status_at=55_000), self.p(host="tmux", pid=20)]), [])

    def test_single_processes_are_left_alone(self):
        self.assertEqual(self.d([self.p(), self.p(uuid="u2", host="tmux", pid=20)]), [])

    def test_external_terminal_is_never_stopped(self):
        self.assertEqual(self.d([self.p(host="external", pid=30), self.p(host="tmux", pid=20)]), [])


class TiedToServer(unittest.TestCase):
    """A chat started on the phone stays tied to the server it was created on: while that server
    runs, every phone message for it starts a second copy on the server, even though the chat
    lives in a window (seen 2026-11-15)."""

    def t(self, entries, children=(), age=600, server="s1"):
        return [e["uuid"] for e in rc.tied_to_server(entries, server, list(children), age, 30)]

    def test_moved_chat_is_untied_once_the_server_is_empty(self):
        self.assertEqual(self.t([entry(server_bound="s1")]), ["u1"])

    def test_server_answering_any_chat_is_not_restarted(self):
        self.assertEqual(self.t([entry(server_bound="s1")], children=["4242"]), [])

    def test_server_that_just_started_is_left_alone(self):
        self.assertEqual(self.t([entry(server_bound="s1")], age=5), [])

    def test_chats_from_an_earlier_server_are_already_untied(self):
        self.assertEqual(self.t([entry(server_bound="s0")]), [])

    def test_only_open_window_sessions_count(self):
        self.assertEqual(self.t([entry(server_bound="s1", state="closed"),
                                 entry(uuid="u2", server_bound="s1", host="server"),
                                 entry(uuid="u3")]), [])

    def test_no_server_running(self):
        self.assertEqual(self.t([entry(server_bound="s1")], server=None), [])


class GhostWindows(unittest.TestCase):
    def p(self, **kw):
        base = dict(uuid="u1", host="tmux", pid=20, status="busy", status_at=0)
        base.update(kw)
        return base

    def test_busy_window_with_a_server_copy_is_a_ghost(self):
        procs = [self.p(), self.p(host="server", pid=10, status="busy")]
        self.assertEqual([p["pid"] for p in rc.ghost_windows(procs)], [20])

    def test_idle_window_is_not(self):
        self.assertEqual(rc.ghost_windows([self.p(status="idle"), self.p(host="server", pid=10)]), [])

    def test_window_running_a_shell_command_is_working_too(self):
        self.assertEqual(len(rc.ghost_windows([self.p(status="shell"), self.p(host="server", pid=10)])), 1)

    def test_busy_window_alone_is_fine(self):
        self.assertEqual(rc.ghost_windows([self.p(), self.p(uuid="u2", host="server", pid=10)]), [])


class PhoneLink(unittest.TestCase):
    """2026-11-17: a chat's window stood down (4090) and stayed off the phone for
    25 minutes. The notice had scrolled off screen, so stood_down() never saw it; the session file
    had lost its bridgeSessionId all along."""

    NOW = 10_000

    def w(self, **kw):
        base = dict(uuid="u1", host="tmux", pid=20, status="idle", status_at=0, link="session_x")
        base.update(kw)
        return base

    def test_window_without_its_link_is_relaunched(self):
        self.assertTrue(rc.phone_lost(entry(link="session_x"), self.w(link=None), self.NOW))

    def test_connected_window_is_left_alone(self):
        self.assertFalse(rc.phone_lost(entry(link="session_x"), self.w(), self.NOW))

    def test_window_still_connecting_gets_a_minute(self):
        self.assertFalse(rc.phone_lost(entry(link="session_x", launched_at=self.NOW - 20), self.w(link=None), self.NOW))

    def test_session_that_never_had_a_link_is_not_a_loss(self):
        self.assertFalse(rc.phone_lost(entry(), self.w(link=None), self.NOW))

    def test_relaunched_at_most_every_five_minutes(self):
        self.assertFalse(rc.phone_lost(entry(link="session_x", phone_relaunch_at=self.NOW - 60),
                                       self.w(link=None), self.NOW))

    def test_list_says_the_phone_is_disconnected(self):
        self.assertIn("PHONE DISCONNECTED", rc.describe(self.w(link=None), entry(link="session_x")))
        self.assertNotIn("DISCONNECTED", rc.describe(self.w(), entry(link="session_x")))

    def test_server_restart_waits_for_new_window_to_settle(self):
        e = entry(server_bound="s1", launched_at=self.NOW - 5)
        settled = {"u1": self.w(status_at=(self.NOW - 30) * 1000)}
        self.assertEqual(rc.tied_to_server([e], "s1", [], 600, 30, settled, self.NOW), [])
        e["launched_at"] = self.NOW - 120
        self.assertEqual(len(rc.tied_to_server([e], "s1", [], 600, 30, settled, self.NOW)), 1)
        for not_ready in (self.w(link=None), self.w(status="busy"), self.w(status_at=(self.NOW - 1) * 1000), None):
            self.assertEqual(rc.tied_to_server([e], "s1", [], 600, 30, {"u1": not_ready}, self.NOW), [], not_ready)


class OneCopyPerSession(unittest.TestCase):
    """Replays 2026-11-15 through the real tick: a chat started on the phone, lives
    in a window; while its window is working, a phone message reaches the server, which starts a
    second copy. Afterwards only one copy may be working, and the chat must be untied from the
    server so the next phone message goes to the window."""

    UUID = "0c0ffee0-0000-4000-8000-000000000001"

    def setUp(self):
        self.calls = []
        self.server = {"invocation": "s1", "children": []}
        self.registry = {self.UUID: dict(uuid=self.UUID, state="open", host="server", invocation="s1",
                                         cwd="/srv", name="chat", link="session_x", absent=0)}
        self.procs = []
        fakes = dict(
            tmux_ready=lambda: True,
            invocations=lambda: {"server": self.server["invocation"], "tmux": "t1", "even": None,
                                 "even_expected": False, "server_restarts": 0, "even_restarts": 0},
            record_all_restarts=lambda *a: None,
            tmux_panes=lambda: [("@9", self.UUID, False, 200, "chat")] if self.window() else [],
            live_processes=lambda panes: [dict(p) for p in self.procs],
            load=lambda: self.registry,
            save=lambda reg: None,
            contracts_paused=lambda: False,
            oom_kills=lambda: 0,
            wrap_now=lambda state, expected: False,
            has_conversation=lambda cwd, uuid: True,
            in_scope=lambda cwd, served: True,
            tmux=lambda *args: self.calls.append(("tmux",) + args),
            stop_process=self.stop,
            launch=self.launch,
            server_children=lambda: list(self.server["children"]),
            seconds_since_start=lambda unit: 600,
            capture_screens=lambda ids: {},
            recent_transcripts=lambda: [],
            adopt_since=lambda: 0,
            transcript_mtime=lambda cwd, uuid: 0,
            slice_memory=lambda: 0,
            sync_windows=lambda reg, panes: None,
            log=lambda msg: self.calls.append(("log", msg)),
        )
        self.saved_attrs = {k: getattr(rc, k) for k in fakes}
        for k, v in fakes.items():
            setattr(rc, k, v)
        self.real_run = rc.subprocess.run
        rc.subprocess.run = self.fake_run

    def tearDown(self):
        for k, v in self.saved_attrs.items():
            setattr(rc, k, v)
        rc.subprocess.run = self.real_run

    # the world the tick sees
    def window(self):
        return next((p for p in self.procs if p["host"] == "tmux"), None)

    def stop(self, proc):
        self.calls.append(("stop", proc["host"]))
        self.procs = [p for p in self.procs if p["pid"] != proc["pid"]]
        self.server["children"] = [c for c in self.server["children"] if c != str(proc["pid"])]
        return True

    def launch(self, entry, wrapped, invs):
        self.calls.append(("launch",))
        entry.update(host="tmux", invocation="t1")
        self.procs = [p for p in self.procs if p["host"] != "tmux"]  # a relaunch replaces the window
        self.procs.append(dict(pid=200, uuid=self.UUID, host="tmux", status="idle", status_at=0,
                               cwd="/srv", name="chat", link="session_x"))
        return True

    def fake_run(self, cmd, **kw):
        self.calls.append(("run",) + tuple(cmd))
        if cmd[:3] == ["systemctl", "--user", "restart"] and cmd[3] == rc.SERVER_UNIT:
            self.server.update(invocation="s2", children=[])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def phone_message_reaches_server(self, status="busy", at=None):
        self.procs.append(dict(pid=100, uuid=self.UUID, host="server", status=status,
                               status_at=at if at is not None else rc.time.time() * 1000,
                               cwd="/srv", name="chat", link="session_x"))
        self.server["children"].append("100")

    def working(self):
        return [p for p in self.procs if p["status"] != "idle"]

    def restarts(self):
        return [c for c in self.calls if c[:4] == ("run", "systemctl", "--user", "restart")]

    def test_moved_chat_is_untied_from_the_server_once(self):
        state = {}
        # 1. The phone chat is answered on the server, goes idle, and is moved into a window.
        self.phone_message_reaches_server(status="idle", at=0)
        rc.tick(state)
        self.assertIn(("launch",), self.calls)
        self.assertEqual(self.registry[self.UUID]["server_bound"], "s1")
        self.assertEqual([p["host"] for p in self.procs], ["tmux"])
        self.assertEqual(self.restarts(), [])  # one change per tick

        # 2. The server is now empty, so the next tick restarts it: the chat is untied.
        rc.tick(state)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(self.server["invocation"], "s2")

        # 3. A restarted server has nothing tied to it, so it is not restarted again.
        state["untied_at"] = 0
        rc.tick(state)
        self.assertEqual(len(self.restarts()), 1)

    def test_server_busy_with_another_chat_is_not_restarted(self):
        state = {}
        self.phone_message_reaches_server(status="idle", at=0)
        rc.tick(state)
        self.server["children"].append("555")  # another phone chat is being answered
        rc.tick(state)
        self.assertEqual(self.restarts(), [])
        self.server["children"].remove("555")
        state["untied_at"] = 0
        rc.tick(state)
        self.assertEqual(len(self.restarts()), 1)

    def test_window_mid_reply_stops_when_a_server_copy_takes_the_phone(self):
        state = {}
        self.launch(self.registry[self.UUID], False, {})
        self.procs[0]["status"] = "busy"  # the window is working on a reply, editing files
        self.phone_message_reaches_server(status="busy")
        self.calls.clear()
        rc.tick(state)
        self.assertIn(("tmux", "send-keys", "-t", "@9", "Escape"), self.calls)
        self.procs[0]["status"] = "idle"  # what Esc does
        self.assertEqual([p["host"] for p in self.working()], ["server"])  # one copy works
        # The busy server copy is answering you: nothing is stopped, launched or restarted.
        self.assertFalse([c for c in self.calls if c[0] in ("stop", "launch", "run")])

        # It finishes and goes idle: it is stopped, the window relaunched and the chat tied to this
        # server, which is restarted on the next tick.
        self.procs[1].update(status="idle", status_at=0)
        self.calls.clear()
        rc.tick(state)
        self.assertEqual([c for c in self.calls if c[0] in ("stop", "launch")], [("stop", "server"), ("launch",)])
        self.assertEqual([p["host"] for p in self.procs], ["tmux"])
        rc.tick(state)
        self.assertEqual(self.server["invocation"], "s2")

    def test_esc_is_sent_once_not_every_tick(self):
        state = {}
        self.launch(self.registry[self.UUID], False, {})
        self.procs[0]["status"] = "busy"
        self.phone_message_reaches_server(status="busy")
        rc.tick(state)
        rc.tick(state)
        self.assertEqual(sum(1 for c in self.calls if c[:2] == ("tmux", "send-keys")), 1)

    def test_window_that_lost_its_phone_link_is_relaunched_once(self):
        state = {}
        self.launch(self.registry[self.UUID], False, {})
        self.registry[self.UUID].update(launched_at=0, server_bound="s0")
        self.procs[0]["link"] = None  # stood down; the notice scrolled off screen
        self.calls.clear()
        rc.tick(state)
        self.assertEqual([c for c in self.calls if c[0] in ("stop", "launch")], [("stop", "tmux"), ("launch",)])
        self.procs[0]["link"] = None  # still no link (offline?): not relaunched again every tick
        self.calls.clear()
        rc.tick(state)
        self.assertFalse([c for c in self.calls if c[0] in ("stop", "launch")])


class StoodDown(unittest.TestCase):
    SCREEN = """
● Remote Control disconnected — another connection took over this
  session (usually another device or Claude Code session) — this device
  is standing down (code 4090)
────────────────────────────────────────────────────────────────────────────────
❯
"""

    def test_window_that_lost_its_phone_connection_is_seen(self):
        self.assertTrue(rc.stood_down(self.SCREEN))

    def test_normal_window_is_fine(self):
        self.assertFalse(rc.stood_down("● /remote-control is active\n❯ \n"))

    def test_reconnected_after_standing_down_is_fine(self):
        self.assertFalse(rc.stood_down(self.SCREEN + "\n● /remote-control is active · Continue here\n❯\n"))


class Selection(unittest.TestCase):
    def test_newest_first_up_to_limit(self):
        c = [dict(uuid="old", mtime=1), dict(uuid="new", mtime=3), dict(uuid="mid", mtime=2)]
        self.assertEqual([x["uuid"] for x in rc.select_reopen(c, live_count=4, limit=6, now=10, max_age=100)],
                         ["new", "mid"])

    def test_dormant_sessions_are_skipped(self):
        c = [dict(uuid="old", mtime=1)]
        self.assertEqual(rc.select_reopen(c, live_count=0, limit=6, now=1000, max_age=100), [])


class Revive(unittest.TestCase):
    def test_forgotten_entry_comes_back_closed_with_its_link_and_title(self):
        e = rc.revive_entry(dict(uuid="u1", state="forgotten", cwd="/a"), "u1", "/a",
                            "session_X", "rctest")
        self.assertEqual((e["state"], e["link"], e["title"], e["name"], e["cwd"]),
                         ("closed", "session_X", "rctest", "rctest", "/a"))

    def test_untracked_session_without_link_or_title_is_named_by_id(self):
        e = rc.revive_entry(None, "abcdef12-0000", "/a", None, None)
        self.assertEqual(e["name"], "abcdef12")
        self.assertNotIn("link", e)
        self.assertNotIn("title", e)

    def test_known_link_wins_over_transcript_link(self):
        e = rc.revive_entry(dict(uuid="u1", link="session_OLD"), "u1", "/a", "session_NEW", None)
        self.assertEqual(e["link"], "session_OLD")

    def test_title_falls_back_to_what_the_old_entry_knew(self):
        e = rc.revive_entry(dict(uuid="u1", title="Old title"), "u1", "/a", None, None)
        self.assertEqual((e["name"], e["title"]), ("Old title", "Old title"))


class LocateTranscript(unittest.TestCase):
    def setUp(self):
        import tempfile, json
        self.dir = tempfile.mkdtemp()
        self.saved = rc.PROJECTS_DIR
        rc.PROJECTS_DIR = self.dir
        os.makedirs(f"{self.dir}/-w")
        self.uuid = "f00dcafe-1111-4222-8333-444455556666"
        with open(f"{self.dir}/-w/{self.uuid}.jsonl", "w") as fh:
            fh.write(json.dumps({"type": "summary"}) + "\n")
            fh.write(json.dumps({"type": "user", "cwd": self.dir}) + "\n")
            fh.write(json.dumps({"type": "bridge-session", "bridgeSessionId": "cse_A"}) + "\n")
            fh.write(json.dumps({"type": "bridge-session", "bridgeSessionId": "cse_B"}) + "\n")

    def tearDown(self):
        rc.PROJECTS_DIR = self.saved

    def test_finds_uuid_folder_and_latest_link_from_a_prefix(self):
        self.assertEqual(rc.locate_transcript("f00dcafe"), (self.uuid, self.dir, "session_B"))

    def test_unknown_id_is_refused(self):
        with self.assertRaises(SystemExit):
            rc.locate_transcript("00000000")

    def test_a_name_is_not_an_id(self):
        with self.assertRaises(SystemExit):
            rc.locate_transcript("rctest")



class Titles(unittest.TestCase):
    """Phone titles are fetched before the lock is taken and applied inside the tick."""

    def test_only_open_sessions_with_a_link_and_a_stale_title_are_due(self):
        reg = {"a": dict(uuid="a", state="open", link="session_a", title_at=0),
               "b": dict(uuid="b", state="open", link="session_b", title_at=1000),
               "c": dict(uuid="c", state="closed", link="session_c"),
               "d": dict(uuid="d", state="open")}
        self.assertEqual(rc.titles_due(reg, now=1000 + rc.TITLE_REFRESH_SECONDS), ["session_a"])

    def test_at_most_two_per_tick(self):
        reg = {str(i): dict(uuid=str(i), state="open", link=f"session_{i}") for i in range(5)}
        self.assertEqual(len(rc.titles_due(reg, now=10**9)), 2)

    def test_fetched_title_is_applied_and_a_failed_fetch_keeps_the_old_one(self):
        reg = {"a": dict(uuid="a", state="open", link="session_a", title="old"),
               "b": dict(uuid="b", state="open", link="session_b", title="keep")}
        rc.apply_titles(reg, {"session_a": "new", "session_b": None}, now=50)
        self.assertEqual((reg["a"]["title"], reg["b"]["title"]), ("new", "keep"))
        self.assertEqual(reg["b"]["title_at"], 50)  # not retried every tick

    def test_a_session_forgotten_meanwhile_is_left_alone(self):
        reg = {"a": dict(uuid="a", state="forgotten")}
        rc.apply_titles(reg, {"session_a": "new"}, now=50)
        self.assertNotIn("title", reg["a"])


class Describe(unittest.TestCase):
    def test_open_but_not_running_under_the_cap_is_starting(self):
        self.assertEqual(rc.describe(None, dict(uuid="u", state="open"), running=2, limit=6), "starting")

    def test_open_but_not_running_at_the_cap_is_queued(self):
        self.assertIn("queued", rc.describe(None, dict(uuid="u", state="open"), running=6, limit=6))

    def test_failed_says_how_to_fix_it(self):
        self.assertIn("reopen", rc.describe(None, dict(uuid="u", state="failed")))

    def test_closed_is_closed(self):
        self.assertEqual(rc.describe(None, dict(uuid="u", state="closed")), "closed")


class Untracked(unittest.TestCase):
    def t(self, uuid, mtime):
        return dict(uuid=uuid, mtime=mtime)

    def test_forgotten_and_never_tracked_are_listed_newest_first(self):
        reg = {"f": dict(uuid="f", state="forgotten"), "o": dict(uuid="o", state="open")}
        rows = rc.untracked(reg, [self.t("f", 10), self.t("o", 30), self.t("n", 20)], now=40, max_age=100)
        self.assertEqual([r["uuid"] for r in rows], ["n", "f"])

    def test_old_chats_are_left_out(self):
        self.assertEqual(rc.untracked({}, [self.t("n", 0)], now=1000, max_age=100), [])


class EvenOptional(unittest.TestCase):
    """Even Terminal (the glasses) is optional: install.sh --with-even."""

    def test_up_means_wrapped(self):
        self.assertTrue(rc.wrap_choice(up=True, expected=True, waited=0))

    def test_not_installed_means_no_wait_and_no_wrapper(self):
        self.assertIs(rc.wrap_choice(up=False, expected=False, waited=0), False)

    def test_installed_but_down_waits_then_goes_without(self):
        self.assertIsNone(rc.wrap_choice(up=False, expected=True, waited=10, limit=90))
        self.assertIs(rc.wrap_choice(up=False, expected=True, waited=90, limit=90), False)

    def wrapped_entry(self):
        return entry(wrapped=True, even="e1", launched_at=0)

    def test_wrapped_session_waits_for_an_installed_even_to_come_back(self):
        invs = dict(server="s1", tmux="t1", even=None, even_expected=True)
        self.assertEqual(rc.decide(self.wrapped_entry(), set(), invs, False, now=1000), "none")

    def test_wrapped_session_is_reopened_once_even_is_removed(self):
        invs = dict(server="s1", tmux="t1", even=None, even_expected=False)
        self.assertEqual(rc.decide(self.wrapped_entry(), set(), invs, False, now=1000), "reopen")


class Rewrap(unittest.TestCase):
    def proc(self, **kw):
        base = dict(host="tmux", status="idle", status_at=0)
        base.update(kw)
        return base

    def test_session_started_without_even_goes_back_once_idle(self):
        self.assertTrue(rc.should_rewrap(dict(state="open", rewrap=True), self.proc(), True, 60_000, 5_000))

    def test_not_while_even_is_still_down(self):
        self.assertFalse(rc.should_rewrap(dict(state="open", rewrap=True), self.proc(), False, 60_000, 5_000))

    def test_not_while_busy_or_just_finished(self):
        e = dict(state="open", rewrap=True)
        self.assertFalse(rc.should_rewrap(e, self.proc(status="busy"), True, 60_000, 5_000))
        self.assertFalse(rc.should_rewrap(e, self.proc(status_at=58_000), True, 60_000, 5_000))

    def test_deliberately_unwrapped_session_is_left_alone(self):
        self.assertFalse(rc.should_rewrap(dict(state="open"), self.proc(), True, 60_000, 5_000))


class TickCost(unittest.TestCase):
    """The tick runs every 5 s, so it batches its systemctl and tmux calls."""

    def test_parse_show_splits_units(self):
        text = "Id=a.service\nActiveState=active\nInvocationID=x\n\nId=b.service\nActiveState=inactive\nInvocationID=\n"
        shown = rc.parse_show(text)
        self.assertEqual(shown["a.service"]["InvocationID"], "x")
        self.assertEqual(shown["b.service"]["ActiveState"], "inactive")
        self.assertEqual(shown["b.service"]["InvocationID"], "")

    def test_even_expected_from_shown_props(self):
        self.assertTrue(rc.even_expected({"UnitFileState": "linked", "ActiveState": "inactive"}))
        self.assertTrue(rc.even_expected({"UnitFileState": "disabled", "ActiveState": "activating"}))
        self.assertFalse(rc.even_expected({"UnitFileState": "", "ActiveState": "inactive"}))

    def test_one_capture_call_for_many_windows(self):
        args = rc.capture_args(["@1", "@2"])
        self.assertEqual(args.count("capture-pane"), 2)
        self.assertEqual(args[:4], ["capture-pane", "-p", "-t", "@1"])
        out = "screen one\n" + rc.CAPTURE_SEP + "\nscreen two\n"
        self.assertEqual(rc.split_captures(["@1", "@2"], out), {"@1": "screen one\n", "@2": "screen two\n"})
        self.assertEqual(rc.split_captures([], ""), {})

    def test_trust_prompt_only_looked_for_in_new_windows(self):
        self.assertTrue(rc.trust_check_due(dict(launched_at=1000), 1060))
        self.assertFalse(rc.trust_check_due(dict(launched_at=1000), 1000 + rc.TRUST_CHECK_SECONDS))
        self.assertFalse(rc.trust_check_due(dict(), 1000))

    def test_save_skips_unchanged_registry(self):
        import tempfile
        old = rc.REGISTRY
        rc.REGISTRY = os.path.join(tempfile.mkdtemp(), "registry.json")
        try:
            rc.save({"u1": {"state": "open"}})
            before = os.stat(rc.REGISTRY).st_mtime_ns
            os.utime(rc.REGISTRY, ns=(before - 10**9, before - 10**9))
            rc.save({"u1": {"state": "open"}})
            self.assertEqual(os.stat(rc.REGISTRY).st_mtime_ns, before - 10**9)
            rc.save({"u1": {"state": "closed"}})
            self.assertEqual(rc.load(), {"u1": {"state": "closed"}})
        finally:
            rc.REGISTRY = old


class Config(unittest.TestCase):
    def write(self, data):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        with open(path, "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_no_file_means_the_defaults(self):
        config, problems = rc.load_config(os.path.join(tempfile.mkdtemp(), "missing.json"))
        self.assertEqual((config, problems), (rc.CONFIG_DEFAULTS, []))

    def test_the_unofficial_api_is_off_by_default(self):
        self.assertFalse(rc.CONFIG_DEFAULTS["archive_sync"])
        self.assertFalse(rc.CONFIG_DEFAULTS["phone_titles"])
        self.assertEqual(rc.CONFIG_DEFAULTS["auto_trust_dirs"], [])

    def test_values_override_the_defaults(self):
        config, problems = rc.load_config(self.write({"max_open": 3, "archive_sync": True, "auto_trust_dirs": ["~"]}))
        self.assertEqual(problems, [])
        self.assertEqual((config["max_open"], config["archive_sync"], config["auto_trust_dirs"]), (3, True, ["~"]))
        self.assertEqual(config["even_port"], 3456)

    def test_unknown_keys_and_wrong_types_are_reported_and_ignored(self):
        config, problems = rc.load_config(self.write(
            {"max_opn": 3, "max_open": "3", "archive_sync": 1, "idle_seconds": True, "auto_trust_dirs": [1]}))
        self.assertEqual(len(problems), 5)
        self.assertEqual(config, rc.CONFIG_DEFAULTS)

    def test_zero_or_negative_numbers_are_refused(self):
        config, problems = rc.load_config(self.write({"max_open": 0}))
        self.assertEqual((config["max_open"], len(problems)), (6, 1))

    def test_broken_json_keeps_the_defaults(self):
        config, problems = rc.load_config(self.write("{not json"))
        self.assertEqual(config, rc.CONFIG_DEFAULTS)
        self.assertEqual(len(problems), 1)

    def test_counts_must_be_whole_numbers(self):
        config, problems = rc.load_config(self.write({"max_open": 7.5, "memory_max_gb": 7.5}))
        self.assertEqual((config["max_open"], config["memory_max_gb"], len(problems)), (6, 7.5, 1))


class ConfigSet(unittest.TestCase):
    # `afield config set <key> <words...>`, added 2026-11-12.
    def test_every_key_says_what_it_does_and_how_it_applies(self):
        self.assertEqual(set(rc.CONFIG_HELP), set(rc.CONFIG_DEFAULTS))
        self.assertTrue(all(how in rc.CONFIG_APPLY for _text, how in rc.CONFIG_HELP.values()))

    def test_numbers(self):
        self.assertEqual(rc.parse_config_value("max_open", ["10"]), 10)
        self.assertEqual(rc.parse_config_value("memory_max_gb", ["7.5"]), 7.5)
        for words in (["0"], ["-1"], ["2.5"], ["ten"], ["1", "2"], []):
            with self.assertRaises(ValueError):
                rc.parse_config_value("max_open", words)

    def test_booleans(self):
        for text, want in (("true", True), ("On", True), ("yes", True), ("false", False), ("off", False)):
            self.assertIs(rc.parse_config_value("archive_sync", [text]), want)
        with self.assertRaises(ValueError):
            rc.parse_config_value("archive_sync", ["1"])

    def test_lists(self):
        self.assertEqual(rc.parse_config_value("auto_trust_dirs", ["~", "~/projects"]), ["~", "~/projects"])
        self.assertEqual(rc.parse_config_value("auto_trust_dirs", ["~,/srv"]), ["~", "/srv"])
        self.assertEqual(rc.parse_config_value("auto_trust_dirs", ['["~"]']), ["~"])
        self.assertEqual(rc.parse_config_value("auto_trust_dirs", ["[]"]), [])
        for words in ([], ["[1]"], ["[not json"]):
            with self.assertRaises(ValueError):
                rc.parse_config_value("auto_trust_dirs", words)

    def test_strings_and_unknown_keys(self):
        self.assertEqual(rc.parse_config_value("served_dir", ["~/rc"]), "~/rc")
        with self.assertRaises(ValueError):
            rc.parse_config_value("served_dir", [""])
        with self.assertRaises(ValueError):
            rc.parse_config_value("max_opn", ["3"])

    def test_a_parsed_value_loads_back(self):
        for key, words in (("max_open", ["10"]), ("archive_sync", ["on"]), ("auto_trust_dirs", ["~"])):
            path = Config().write({key: rc.parse_config_value(key, words)})
            self.assertEqual(rc.load_config(path)[1], [])

    def test_help_lists_every_key_with_its_value(self):
        text = rc.config_help(dict(rc.CONFIG_DEFAULTS, max_open=10), "/x/config.json")
        self.assertIn("afield config set <key>", text)
        for key in rc.CONFIG_DEFAULTS:
            self.assertIn(key, text)
        self.assertIn("max_open             10  (default 6; supervisor)", text)


class EvenHealth(unittest.TestCase):
    # 2026-11-10: Even crash-looped 298 times while status said only "activating, port DOWN".
    def test_a_quiet_even_is_fine(self):
        self.assertTrue(rc.even_health([1, 2, 3], 10_000)[0])

    def test_a_crash_loop_is_reported(self):
        ok, words = rc.even_health([9_000, 9_500, 9_900], 10_000)
        self.assertFalse(ok)
        self.assertIn("RESTARTING (3 times in the last hour)", words)
        self.assertIn("journalctl --user -u even-terminal.service", words)

    def test_even_restarts_are_kept_apart_from_the_servers(self):
        path = os.path.join(tempfile.mkdtemp(), "health.json")
        state = {}
        old_log, rc.log = rc.log, lambda _m: None
        try:
            rc.record_server_restarts(state, 0, 100, path)
            rc.record_restarts(state, "even", 5, 100, path)   # first look
            rc.record_restarts(state, "even", 7, 110, path)   # two more
            rc.record_server_restarts(state, 1, 120, path)
        finally:
            rc.log = old_log
        health = rc.read_server_health(path)
        self.assertEqual(health["even_restarts"], [110, 110])
        self.assertEqual(health["restarts"], [120])


class ServerHealth(unittest.TestCase):
    def test_active_with_no_restarts_is_ok(self):
        self.assertEqual(rc.server_health("active", [], 10_000)[0], True)

    def test_old_restarts_do_not_count(self):
        self.assertTrue(rc.server_health("active", [1, 2, 3, 4], 10_000)[0])

    def test_a_crash_loop_is_reported(self):
        ok, words = rc.server_health("active", [9_000, 9_500, 9_900], 10_000)
        self.assertFalse(ok)
        self.assertIn("RESTARTING (3 times", words)

    def test_a_stopped_server_is_reported(self):
        self.assertFalse(rc.server_health("failed", [], 10_000)[0])

    def test_restarts_are_recorded_once_each(self):
        path = os.path.join(tempfile.mkdtemp(), "health.json")
        state, logged = {}, []
        old_log, rc.log = rc.log, logged.append
        try:
            rc.record_server_restarts(state, 2, 100, path)   # first look: nothing new
            rc.record_server_restarts(state, 2, 105, path)
            rc.record_server_restarts(state, 4, 110, path)   # two more
            rc.record_server_restarts(state, 0, 115, path)   # counter reset by a reload
            rc.record_server_restarts(state, 1, 120, path)
        finally:
            rc.log = old_log
        self.assertEqual(rc.read_server_health(path)["restarts"], [110, 110, 120])
        self.assertEqual(len(logged), 2)

    def test_unknown_count_is_ignored(self):
        state = {}
        rc.record_server_restarts(state, None, 100, os.path.join(tempfile.mkdtemp(), "h.json"))
        self.assertEqual(state, {})


class Memory(unittest.TestCase):
    def test_own_process_has_a_size(self):
        self.assertGreater(rc.rss_bytes(os.getpid()), 0)

    def test_gone_process_has_none(self):
        self.assertIsNone(rc.rss_bytes(2**22 + 12345))

    def test_inactive_file_cache_does_not_count(self):
        stat = "anon 1800\nfile 2400\ninactive_file 2100\nactive_file 300\n"
        self.assertEqual(rc.working_set(4400, stat), 2300)

    def test_no_stat_means_all_of_it(self):
        self.assertEqual(rc.working_set(4400, ""), 4400)
        self.assertEqual(rc.working_set(100, "inactive_file 300\n"), 0)

    def test_words(self):
        self.assertEqual(rc.memory_words(450 * 1024**2), "450 MB")
        self.assertEqual(rc.memory_words(int(1.5 * 1024**3)), "1.5 GB")
        self.assertEqual(rc.memory_words(None), "")


class GlassesList(unittest.TestCase):
    # 2026-11-10: the glasses listed every project's sessions, afield's under "claude-rc-eb".
    REGISTRY = {
        "a": {"uuid": "a", "state": "open", "title": "Site redesign", "name": "claude-rc-eb"},
        "b": {"uuid": "b", "state": "open", "name": "claude-rc-32"},
        "c": {"uuid": "c", "state": "forgotten"},
    }

    def test_only_open_sessions_under_their_phone_titles(self):
        upstream = [{"id": "x", "title": "other project"}, {"id": "a", "title": "claude-rc-eb", "status": "idle"},
                    {"id": "c", "title": "retired"}, {"id": "b", "title": "claude-rc-32"}]
        got = rc.glasses_sessions(upstream, self.REGISTRY, 10)
        self.assertEqual([(s["id"], s["title"]) for s in got], [("a", "Site redesign"), ("b", "claude-rc-32")])
        self.assertEqual(got[0]["status"], "idle")  # everything else Even sent is kept

    def test_the_glasses_limit_is_kept(self):
        upstream = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(len(rc.glasses_sessions(upstream, self.REGISTRY, 1)), 1)

    def test_list_request_asks_even_for_more(self):
        path, limit = rc.glasses_list_request("/api/sessions?provider=claude")
        self.assertEqual(limit, 10)  # Even's own default
        self.assertIn("provider=claude", path)
        self.assertIn(f"limit={rc.GLASSES_FETCH}", path)
        self.assertEqual(rc.glasses_list_request("/api/sessions?limit=3&token=t")[1], 3)
        self.assertIsNone(rc.glasses_list_request("/api/sessions/a/history?limit=10"))
        self.assertIsNone(rc.glasses_list_request("/api/info"))

    def test_only_authenticated_api_routes_are_passed_on(self):
        # Even trusts 127.0.0.1 for its hook endpoint and its wrapper WebSocket; through the proxy
        # every request comes from 127.0.0.1, so those must never be forwarded.
        for path in ("/api/sessions?provider=claude", "/api/events", "/api/prompt", "/api/sessions/a/history"):
            self.assertTrue(rc.glasses_may_forward(path, {}), path)
        for path in ("/api/claude-sync/hook", "/API/Claude-Sync/hook", "/api/claude%2Dsync/hook",
                     "//api/claude-sync/hook", "/api/claude-sync/hook/", "/", "/claude-sync", "/api/../x"):
            self.assertFalse(rc.glasses_may_forward(path, {}), path)
        self.assertFalse(rc.glasses_may_forward("/api/events", {"Upgrade": "websocket"}))

    def test_outside_address_follows_evens_network_mode(self):
        run = lambda args, **_k: subprocess.CompletedProcess(args, 0, stdout="100.1.2.3\nfd7a::1\n")
        self.assertEqual(rc.even_outside_address({"mode": "tailscale"}, run=run), "100.1.2.3")
        ip = lambda args, **_k: subprocess.CompletedProcess(args, 0, stdout=(
            "1: lo    inet 127.0.0.1/8 scope host lo\n"
            "2: eth0    inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0\n"))
        self.assertEqual(rc.even_outside_address({"mode": "lan"}, run=ip), "192.168.1.20")
        self.assertEqual(rc.even_outside_address({"mode": "interface", "name": "eth0"}, run=ip), "192.168.1.20")
        self.assertIsNone(rc.even_outside_address({"mode": "expose", "provider": "ngrok"}, run=ip))


RULE = "─" * 80
# Screens captured from the windows on 2026-11-19, where glasses messages were being lost.
OFFER = f"""● Sent. Monitor is running.
{RULE}
  Teach auto mode about your environment?

  Auto mode works better when it knows your environment. Takes about a minute.

  ❯ 1. Yes
    2. Not now
    3. Don't show again

  Enter to confirm · Esc to cancel
{RULE}
❯ Add a day trip on Saturday
{RULE}
  ⏵⏵ auto mode on (shift+tab to cycle)
"""
WIZARD_TOGGLES = """  Should I push feat/sis-implementation now?
✻ Brewed for 23s · done 4:46 AM
  Teach auto mode about your environment?
  Claude Code reads this project, your recent Claude sessions, and optionally
  your shell history and other repositories. Claude analyzes this data and
  customizes auto mode to make better decisions.
    How you use Claude here     Mixed
  ❯ Also scan shell history     on
    Also scan your other repos  off
    Continue
  ↑/↓ to navigate · Space to toggle · Esc to cancel
"""
WIZARD_ARROWS = WIZARD_TOGGLES.replace("     on", "     true").replace(
    "↑/↓ to navigate · Space to toggle · Esc to cancel", "←/→ to change · Enter to continue · Esc to cancel")
# This conversation, on screen in its own window: it quotes the pop-up but is not it.
QUOTED = f"""● The pop-up reads "Teach auto mode about your environment?" with three options:
  Teach auto mode about your environment?
  1. Yes, 2. Not now, 3. Don't show again. Press Esc to cancel.
{RULE}
❯
{RULE}
  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt
"""


class EnvSetupPopup(unittest.TestCase):
    """2026-11-19: Claude Code's "Teach auto mode about your environment?" pop-up sat in five
    windows. The glasses type a message and press Enter; the Enter answered the pop-up (or started
    its setup) and the message never reached the chat."""

    def test_recognises_the_offer_and_both_wizard_screens(self):
        self.assertEqual(rc.env_setup_prompt(OFFER), "offer")
        self.assertEqual(rc.env_setup_prompt(WIZARD_TOGGLES), "wizard")
        self.assertEqual(rc.env_setup_prompt(WIZARD_ARROWS), "wizard")

    def test_a_chat_quoting_the_pop_up_is_left_alone(self):
        self.assertIsNone(rc.env_setup_prompt(QUOTED))
        self.assertIsNone(rc.env_setup_prompt("❯ \n  ⏵⏵ auto mode on\n"))

    def test_a_pop_up_scrolled_up_by_later_output_is_left_alone(self):
        later = OFFER + "\n".join(f"● line {i}" for i in range(20))
        self.assertIsNone(rc.env_setup_prompt(later))

    def answer(self, screens, options=("1. Yes", "2. Not now", "3. Don't show again"), start=0):
        calls = []
        at = {}  # window -> index of the selected option; Down moves it, stopping at the last

        class Out:
            def __init__(self, stdout):
                self.stdout = stdout

        def tmux(*args):
            calls.append(args)
            w = args[-2] if args[0] == "send-keys" else args[-1]
            if args[0] == "send-keys" and args[-1] == "Down":
                at[w] = min(at.get(w, start) + 1, len(options) - 1)
            i = at.get(w, start)
            return Out("".join(("  ❯ " if n == i else "    ") + o + "\n" for n, o in enumerate(options)))
        fakes = dict(capture_screens=lambda ids: {w: screens[w] for w in ids if w in screens},
                     tmux=tmux, log=lambda msg: calls.append(("log", msg)))
        saved = {k: getattr(rc, k) for k in fakes}
        saved_sleep = rc.time.sleep
        try:
            for k, v in fakes.items():
                setattr(rc, k, v)
            rc.time.sleep = lambda s: None
            registry = {"u1": entry(name="Trip planning"), "u2": entry(uuid="u2", name="Ratings"),
                        "u3": entry(uuid="u3", name="this chat")}
            panes = [("@6", "u1", False, 1, "x"), ("@9", "u2", False, 2, "y"), ("@4", "u3", False, 3, "z")]
            state = {}
            n = rc.dismiss_env_setup(state, registry, panes)
            again = rc.dismiss_env_setup(state, registry, panes)  # the next tick, 5 s later
        finally:
            for k, v in saved.items():
                setattr(rc, k, v)
            rc.time.sleep = saved_sleep
        return n, again, [c for c in calls if c[0] == "send-keys"], calls

    def test_offer_gets_dont_show_again_and_wizard_gets_esc(self):
        n, again, keys, _ = self.answer({"@6": OFFER, "@9": WIZARD_TOGGLES, "@4": QUOTED})
        self.assertEqual(n, 2)
        by_window = {w: [k[3] for k in keys if k[2] == w] for w in ("@6", "@9", "@4")}
        self.assertEqual(by_window, {"@6": ["Down", "Down", "Enter"], "@9": ["Escape"], "@4": []})
        self.assertEqual(again, 0)  # not answered twice while the window redraws

    def test_already_on_dont_show_again_just_confirms(self):
        # 2026-11-19: the selection had been moved to option 3 by hand.
        _, _, keys, _ = self.answer({"@6": OFFER}, start=2)
        self.assertEqual([k[3] for k in keys], ["Enter"])

    def test_enter_only_when_dont_show_again_is_selected(self):
        _, _, keys, calls = self.answer({"@6": OFFER}, options=("1. Yes", "2. Not now", "3. Never"))
        self.assertNotIn("Enter", [k[3] for k in keys])
        self.assertEqual(keys[-1], ("send-keys", "-t", "@6", "Escape"))
        self.assertTrue(any("screen-2" in c[1] for c in calls if c[0] == "log"))


class ContractAlerts(unittest.TestCase):
    """Contract failures reach your phone, through your webhook, once each."""

    FAIL = {"id": "SCREEN-2", "status": "FAIL", "detail": ["'Esc to cancel' no longer appears"]}
    CHANGED = {"id": "API-1", "status": "changed", "detail": ["item: +permission_mode"]}
    OK = {"id": "CLI-1", "status": "ok", "detail": []}

    def test_all_passing_from_the_start_sends_nothing(self):
        self.assertIsNone(rc.contract_alert([self.OK], None))

    def test_failure_is_sent_once_then_recovery(self):
        key, title, text = rc.contract_alert([self.OK, self.FAIL, self.CHANGED], None)
        self.assertEqual(title, "afield: 1 contract FAILED")
        self.assertTrue(text.startswith("SCREEN-2: 'Esc to cancel'"))
        self.assertNotIn("API-1", text)
        self.assertIsNone(rc.contract_alert([self.CHANGED, self.FAIL, self.OK], key))  # same again
        worse = dict(self.FAIL, detail=["'Also scan shell history' no longer appears"])
        self.assertIsNotNone(rc.contract_alert([worse], key))  # a new detail is news
        back = rc.contract_alert([self.OK], key)
        self.assertEqual(back[1], "afield: contracts pass again")
        self.assertIsNone(rc.contract_alert([self.OK], back[0]))

    def test_changed_alone_is_not_sent(self):
        # 2026-11-19: API-2 flips between ok and changed as optional fields come and go.
        self.assertIsNone(rc.contract_alert([self.CHANGED], None))
        self.assertIsNone(rc.contract_alert([self.OK], None))

    def test_signed_as_the_webhook_checks_it(self):
        import hashlib, hmac
        secret = os.path.join(tempfile.mkdtemp(), "s")
        with open(secret, "w") as fh:
            fh.write("sekrit\n")
        sent = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        real = rc.urllib.request.urlopen
        rc.urllib.request.urlopen = lambda req, timeout: sent.append(req) or Resp()
        try:
            self.assertEqual(rc.send_alert("t", "x", url="https://example.invalid/", secret_path=secret), 200)
        finally:
            rc.urllib.request.urlopen = real
        req = sent[0]
        stamp, body = req.get_header("X-timestamp"), req.data
        # what a receiver verifying X-Signature computes
        expected = hmac.new(b"sekrit", stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertEqual(req.get_header("X-signature"), expected)
        self.assertEqual(json.loads(body)["title"], "t")

    def test_a_failed_send_is_retried_later_not_every_tick(self):
        d = tempfile.mkdtemp()
        calls = []

        def send(title, text):
            calls.append(title)
            if len(calls) == 1:
                raise OSError("offline")
        saved = {k: getattr(rc, k) for k in ("ALERTS_URL", "ALERTS_STATE", "saved_contracts", "send_alert", "log")}
        rc.ALERTS_URL, rc.ALERTS_STATE = "https://example.invalid/", os.path.join(d, "alerts.json")
        rc.saved_contracts = lambda: {"results": [self.FAIL]}
        rc.send_alert, rc.log = send, lambda m: None
        try:
            state = {}
            rc.notify_contracts(state, now=1000)            # offline
            rc.notify_contracts(state, now=1005)            # next tick: waits
            rc.notify_contracts(state, now=1000 + rc.ALERT_RETRY_SECONDS)  # retried, sent
            rc.notify_contracts(state, now=5000)            # already sent
        finally:
            for k, v in saved.items():
                setattr(rc, k, v)
        self.assertEqual(len(calls), 2)

    def test_alerts_url_must_be_https_or_this_machine(self):
        self.assertTrue(rc.config_value_ok("alerts_url", "https://hooks.example.com/abc"))
        self.assertTrue(rc.config_value_ok("alerts_url", "http://localhost:8080/afield"))
        self.assertTrue(rc.config_value_ok("alerts_url", "http://127.0.0.1/x"))
        self.assertFalse(rc.config_value_ok("alerts_url", "http://example.com/x"))
        self.assertFalse(rc.config_value_ok("alerts_url", "http://localhost.evil.com/x"))
        self.assertTrue(rc.config_value_ok("alerts_format", "ntfy"))
        self.assertFalse(rc.config_value_ok("alerts_format", "carrier-pigeon"))

    def test_formats(self):
        body, headers = rc.alert_request("json", "T", "x", None, 100, "afield on h")
        self.assertEqual(json.loads(body), {"source": "afield on h", "title": "T", "text": "x"})
        self.assertNotIn("X-Signature", headers)  # no secret file: unsigned
        body, headers = rc.alert_request("ntfy", "T", "x", "tk_1", 100, "s")
        self.assertEqual((headers["Title"], headers["Authorization"]), ("T", "Bearer tk_1"))
        self.assertTrue(body.startswith(b"x"))
        self.assertNotIn("Authorization", rc.alert_request("ntfy", "T", "x", None, 100, "s")[1])
        self.assertIn("*T*", json.loads(rc.alert_request("slack", "T", "x", None, 100, "s")[0])["text"])
        self.assertIn("**T**", json.loads(rc.alert_request("discord", "T", "x", None, 100, "s")[0])["content"])

    def test_webhook_paths_are_not_printed(self):
        self.assertEqual(rc.masked_url("https://hooks.slack.com/services/T0/B0/xyz"), "https://hooks.slack.com/…")
        self.assertEqual(rc.masked_url("https://ntfy.sh/"), "https://ntfy.sh/")
        self.assertEqual(rc.masked_url(""), "")
        text = rc.config_help(dict(rc.CONFIG_DEFAULTS, alerts_url="https://h.example/secret-path"), "/c.json")
        self.assertNotIn("secret-path", text)


class LoginExpiry(unittest.TestCase):
    NOW = 1795000000.0  # 2026-11-18 11:06:40 UTC

    def info(self, days):
        stamp = rc.datetime.fromtimestamp(self.NOW + days * 86400, rc.timezone.utc).isoformat().replace("+00:00", "Z")
        return {"loggedIn": True, "email": "someone@example.com", "loginExpiresAt": stamp}

    def test_states(self):
        self.assertEqual(rc.login_status(self.info(5), self.NOW)[0], "ok")
        self.assertEqual(rc.login_status(self.info(2.9), self.NOW)[0], "soon")
        self.assertEqual(rc.login_status(self.info(-1), self.NOW)[0], "ended")
        self.assertEqual(rc.login_status({"loggedIn": False}, self.NOW), ("signed_out", None))
        self.assertEqual(rc.login_status(None, self.NOW), ("unknown", None))
        self.assertEqual(rc.login_status({"loggedIn": True}, self.NOW), ("unknown", None))

    def test_words(self):
        state, ends = rc.login_status(self.info(2.5), self.NOW)
        self.assertEqual(rc.login_words(state, ends, self.NOW),
                         "ends 2026-11-20 23:06 UTC (in 2 d 12 h): run `claude auth login` before then")
        # without now there is no countdown, so a repeated alert has the same text
        self.assertEqual(rc.login_words(state, ends), rc.login_words(state, ends))
        self.assertNotIn("(in", rc.login_words(state, ends))
        self.assertNotIn("example.com", rc.login_words(state, ends, self.NOW))

    def test_status_line_names_a_failure_that_pauses_nothing(self):
        saved = {"ran_at": self.NOW, "versions": {}, "blocking": [],
                 "results": [{"id": "AUTH-1", "status": "FAIL"}, {"id": "CLI-1", "status": "ok"}]}
        self.assertTrue(rc.contracts_line(saved, self.NOW).startswith("WARNING: AUTH-1 failed (pauses nothing)"))
        saved["results"][0]["status"] = "ok"
        self.assertTrue(rc.contracts_line(saved, self.NOW).startswith("ok (2 pass)"))

    def test_read_login_survives_a_missing_binary(self):
        self.assertIsNone(rc.read_login("/nonexistent/claude"))


class NewTitle(unittest.TestCase):
    def test_a_title_that_looks_like_a_flag_is_refused(self):
        # the title is passed to claude after the flags; "-p x" would turn into a flag
        with self.assertRaises(SystemExit) as cm:
            rc.cmd_new("-p do something")
        self.assertIn("cannot start with '-'", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
