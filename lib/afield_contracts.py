"""Contract tests: every assumption afield makes about Claude Code, Even Terminal, tmux,
systemd and Linux, checked against the real system.

Why each contract exists, what breaks when it fails and how to fix it: CONTRACTS.md (same IDs).
Run with `afield doctor`. The supervisor runs the static and live levels when a version changes
(and every few hours); a failed critical contract pauses the actions that act on that assumption.

Levels:
  static  reads the installed programs (flags in --help, strings inside the claude binary). Instant,
          no side effects, and works on a version you have not switched to yet (--candidate).
  live    reads the running system (session files, transcripts, tmux, systemd, the sessions API
          with GET only). Read-only.
  e2e     starts a throwaway session on its own tmux server, checks a link is made and kept across
          a resume, archives it, and removes it again. Creates (then archives) one chat on the
          phone, so it only runs on request: `afield doctor --e2e`.

Results: ok · changed (still meets every requirement, but differs from contracts/snapshot.json:
the diff says how) · FAIL (a requirement no longer holds) · skip (nothing to check against right now).

This module never imports afield; afield passes itself in as `rc`, so the checks exercise the
very functions and constants the supervisor uses.
"""
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import urllib.error
import uuid as uuid_mod

OK, CHANGED, FAIL, SKIP = "ok", "changed", "FAIL", "skip"
LEVELS = ("static", "live", "e2e")
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SNAPSHOT = os.path.join(REPO, "contracts", "snapshot.json")
CONTRACTS_DOC = os.path.join(REPO, "CONTRACTS.md")
PAUSES = "moving and adopting chats, archive sync, and transcript link writes"


# --- pure helpers (unit-tested) --------------------------------------------------------------


def help_flags(text):
    """Every --flag named in --help output. `--[no-]x` counts as both --x and --no-x."""
    flags = set()
    for neg, name in re.findall(r"--(\[no-\])?([a-z][a-z0-9-]*)", text):
        flags.add(name)
        if neg:
            flags.add("no-" + name)
    return flags


def scan_file(path, needles, chunk=8 << 20):
    """Which of `needles` (str) occur in the file. Reads in chunks, so a 300 MB binary never sits
    in memory at once; chunks overlap so a needle split across a boundary is still found."""
    wanted = {n: n.encode() for n in needles}
    found, keep = set(), max((len(b) for b in wanted.values()), default=1) - 1
    tail = b""
    with open(path, "rb") as fh:
        while wanted.keys() - found:
            block = fh.read(chunk)
            if not block:
                break
            window = tail + block
            for name, raw in wanted.items():
                if name not in found and raw in window:
                    found.add(name)
            tail = window[-keep:] if keep else b""
    return found


def type_problems(record, spec, where=""):
    """spec: {key: type or tuple of types}. Lists what is missing or of the wrong type."""
    problems = []
    for key, kind in spec.items():
        if key not in record:
            problems.append(f"{where}missing key {key!r}")
        elif not isinstance(record[key], kind) or (kind is int and isinstance(record[key], bool)):
            problems.append(f"{where}{key!r} is {type(record[key]).__name__}, expected "
                            f"{getattr(kind, '__name__', None) or '/'.join(k.__name__ for k in kind)}")
    return problems


def diff_observed(old, new, prefix=""):
    """Human-readable differences between two observations (dicts of lists, scalars, dicts)."""
    lines = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            label = f"{prefix}{key}"
            if key not in old:
                lines.append(f"{label}: new: {_short(new[key])}")
            elif key not in new:
                lines.append(f"{label}: no longer seen (was {_short(old[key])})")
            else:
                lines.extend(diff_observed(old[key], new[key], label + "."))
        return lines
    if isinstance(old, list) and isinstance(new, list):
        a, b = set(map(str, old)), set(map(str, new))
        if a != b:
            parts = [f"+{x}" for x in sorted(b - a)] + [f"-{x}" for x in sorted(a - b)]
            lines.append(f"{prefix.rstrip('.')}: {' '.join(parts)}")
        return lines
    if old != new:
        lines.append(f"{prefix.rstrip('.')}: {_short(old)} -> {_short(new)}")
    return lines


def _short(value):
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= 80 else text[:77] + "..."


def blocking(results, registry=None):
    """IDs of failed critical contracts: while any remain, the risky actions stay paused."""
    registry = registry or {c["id"]: c for c in CONTRACTS}
    return sorted(r["id"] for r in results if r["status"] == FAIL and not r.get("pauses_nothing")
                  and registry.get(r["id"], {}).get("critical", True)
                  and registry.get(r["id"], {}).get("level") != "e2e")  # manual runs never pause the supervisor


def summary_counts(results):
    counts = {s: 0 for s in (OK, CHANGED, FAIL, SKIP)}
    for r in results:
        counts[r["status"]] += 1
    return counts


def compare_to_snapshot(results, snapshot):
    """Marks ok results whose observation differs from the snapshot as `changed`, with the diff."""
    observed = (snapshot or {}).get("observed", {})
    for r in results:
        if r["status"] != OK or r["id"] not in observed or r.get("observed") is None:
            continue
        lines = diff_observed(observed[r["id"]], r["observed"])
        if lines:
            r["status"] = CHANGED
            r["detail"] = lines + r.get("detail", [])
    return results


def format_report(results, versions, registry=None, candidate=False):
    registry = registry or {c["id"]: c for c in CONTRACTS}
    head = " · ".join(f"{k} {v}" for k, v in versions.items() if v)
    lines = [f"afield doctor · {head}"]
    for r in results:
        c = registry.get(r["id"], {})
        lines.append(f"{r['status']:8} {r['id']:8} {c.get('title', '')}")
        show = r.get("detail", []) if r["status"] != OK else r.get("note", [])
        for line in show[:12]:
            lines.append(f"{'':17}{line}")
        if len(show) > 12:
            lines.append(f"{'':17}... {len(show) - 12} more")
        if r["status"] in (FAIL, CHANGED):
            used = ", ".join(c.get("used_by", [])) or "-"
            lines.append(f"{'':17}see CONTRACTS.md#{r['id'].lower()} · used by {used}")
    counts = summary_counts(results)
    lines.append(f"{counts[OK]} ok, {counts[CHANGED]} changed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    stop = blocking(results, registry)
    e2e = [r["id"] for r in results if r["status"] == FAIL and registry.get(r["id"], {}).get("level") == "e2e"]
    if e2e:
        lines.append(f"end-to-end failures ({', '.join(e2e)}) pause nothing automatically: check the "
                     "test first (CONTRACTS.md), then fix afield before relying on moves and reopens")
    if stop:
        lines.append(f"{'if you switch to it, these would pause' if candidate else 'paused until fixed'}: "
                     f"{PAUSES} (failed: {', '.join(stop)})")
    return "\n".join(lines)


def is_stale(saved, fingerprint, now, every=6 * 3600, retry_skips=600):
    """Whether the supervisor should run the checks again."""
    if not saved or saved.get("fingerprint") != fingerprint:
        return True
    age = now - saved.get("ran_at", 0)
    if age >= every:
        return True
    return age >= retry_skips and any(r["status"] == SKIP and not r.get("off") for r in saved.get("results", []))


# --- registry ---------------------------------------------------------------------------------

CONTRACTS = []


def contract(cid, level, title, used_by, critical=True):
    """used_by: the afield functions that rely on it (checked to exist by the unit tests)."""
    def register(fn):
        CONTRACTS.append(dict(id=cid, level=level, title=title, used_by=used_by, critical=critical, fn=fn))
        return fn
    return register


def result(cid, status, detail=(), observed=None):
    return dict(id=cid, status=status, detail=list(detail), observed=observed)


def turned_off(cid, key):
    """A skip because the feature that relies on this contract is off in the config. It is not
    retried every few minutes like other skips."""
    return dict(result(cid, SKIP, [f"not used: {key} is off in the config"]), off=True)


class Ctx:
    """What the checks run against. `claude` may be a candidate binary you have not switched to."""

    def __init__(self, rc, claude=None):
        self.rc = rc
        self.claude = os.path.realpath(claude or shutil.which("claude") or "claude")
        self.even = shutil.which("even-terminal")
        self._help = {}

    def help(self, *args, program=None):
        key = (program or self.claude,) + args
        if key not in self._help:
            out = subprocess.run([program or self.claude, *args, "--help"], capture_output=True,
                                 text=True, timeout=60)
            self._help[key] = out.stdout + out.stderr
        return self._help[key]


def even_package_dir(even_bin):
    """The installed @evenrealities/even-terminal package, found from its executable."""
    if not even_bin:
        return None
    path = os.path.dirname(os.path.realpath(even_bin))
    for _ in range(4):
        if os.path.exists(os.path.join(path, "package.json")):
            return path
        path = os.path.dirname(path)
    return None


def versions(rc, claude=None):
    """{program: version} for the report and the supervisor's change detection."""
    out = {}
    binary = os.path.realpath(claude or shutil.which("claude") or "")
    out["claude"] = os.path.basename(binary) if re.fullmatch(r"\d+\.\d+\.\d+.*", os.path.basename(binary)) \
        else (binary or None)
    pkg = even_package_dir(shutil.which("even-terminal"))
    try:
        with open(os.path.join(pkg, "package.json")) as fh:
            out["even-terminal"] = json.load(fh).get("version")
    except (OSError, ValueError, TypeError):
        out["even-terminal"] = None
    return out


def fingerprint(rc):
    v = versions(rc)
    return f"claude {v['claude']} · even-terminal {v['even-terminal']}"


# --- static -----------------------------------------------------------------------------------

CLAUDE_FLAGS = {"resume", "session-id", "remote-control"}
SERVER_FLAGS = {"no-create-session-in-dir"}


@contract("CLI-1", "static", "claude takes the flags afield starts sessions with",
          ["launch_command"])
def check_cli_flags(ctx):
    flags = help_flags(ctx.help())
    missing = sorted(CLAUDE_FLAGS - flags)
    return result("CLI-1", FAIL if missing else OK, [f"`claude --help` no longer lists --{f}" for f in missing],
                  {"flags": sorted(flags)})


@contract("CLI-2", "static", "the Remote Control server takes --no-create-session-in-dir",
          ["afield.service"])
def check_server_flags(ctx):
    flags = help_flags(ctx.help("remote-control"))
    missing = sorted(SERVER_FLAGS - flags)
    return result("CLI-2", FAIL if missing else OK,
                  [f"`claude remote-control --help` no longer lists --{f}" for f in missing], {"flags": sorted(flags)})


BINARY_NAMES = ["bridgeSessionId", "procStart", "statusUpdatedAt", "sessionId", "bridge-session",
                "sdk-ts", "ccr-byoc-2025-07-29", "/v1/sessions"]


@contract("BIN-1", "static", "the claude binary still uses the field names afield reads",
          ["live_processes", "link_record", "last_link_in_transcript", "should_adopt", "api_request"])
def check_binary_names(ctx):
    found = scan_file(ctx.claude, BINARY_NAMES)
    missing = [n for n in BINARY_NAMES if n not in found]
    return result("BIN-1", FAIL if missing else OK,
                  [f"{n!r} no longer appears in {ctx.claude}" for n in missing], {"found": sorted(found)})


SCREEN_TEXT = ["Yes, I trust this folder", "Accessing workspace:", "standing down",
               "remote-control is active", "❯", "esc to interrupt"]


@contract("SCREEN-1", "static", "the TUI still shows the text afield looks for on screen",
          ["trust_prompt_dir", "answer_trust", "stood_down", "stop_ghost_replies"])
def check_screen_text(ctx):
    found = scan_file(ctx.claude, SCREEN_TEXT)
    missing = [n for n in SCREEN_TEXT if n not in found]
    return result("SCREEN-1", FAIL if missing else OK,
                  [f"{n!r} no longer appears in the claude binary" for n in missing], {"found": sorted(found)})


ENV_SETUP_TEXT = ["Teach auto mode about your environment?", "Esc to cancel", "Also scan shell history",
                  # the options, in the order afield walks them, and what each one does
                  '[{label:"Yes",value:"accept"},{label:"Not now",value:"later"},'
                  '{label:"Don\'t show again",value:"dismiss"}]']


@contract("SCREEN-2", "static", "the auto mode setup pop-up still reads and orders its options as afield expects",
          ["env_setup_prompt", "dismiss_env_setup"], critical=False)
def check_env_setup_text(ctx):
    # Not critical: afield only presses keys on an exact match, so a reworded pop-up is left alone
    # (and glasses messages may be lost again) rather than answered wrongly.
    found = scan_file(ctx.claude, ENV_SETUP_TEXT)
    missing = [n for n in ENV_SETUP_TEXT if n not in found]
    return result("SCREEN-2", FAIL if missing else OK,
                  [f"{n!r} no longer appears in the claude binary" for n in missing], {"found": sorted(found)})


def even_missing(cid, ctx):
    """even-terminal was not found. A skip if Even is not installed; a failure if its unit is enabled,
    since the unit then crash-loops and every session runs without the glasses (2026-11-10)."""
    if ctx.rc.even_expected():
        # Reported, but nothing is paused: sessions still move and reopen, only without the glasses.
        return dict(result(cid, FAIL, [f"even-terminal.service is enabled, but even-terminal is not on afield's PATH "
                                       f"({os.environ.get('PATH', '')}); rerun install.sh, or install.sh --without-even"]),
                    pauses_nothing=True)
    return result(cid, SKIP, ["even-terminal is not installed"])


EVEN_FLAGS = {"log-file", "log-level", "claude-allowed-tools", "port"}


@contract("EVEN-1", "static", "even-terminal takes the flags its unit uses and has `even-terminal claude`",
          ["even-terminal.service", "launch_command"])
def check_even_flags(ctx):
    if not ctx.even:
        return even_missing("EVEN-1", ctx)
    text = ctx.help(program=ctx.even)
    flags = help_flags(text)
    problems = [f"`even-terminal --help` no longer lists --{f}" for f in sorted(EVEN_FLAGS - flags)]
    if not re.search(r"even-terminal claude\b", text):
        problems.append("`even-terminal --help` no longer lists the `claude` command")
    return result("EVEN-1", FAIL if problems else OK, problems, {"flags": sorted(flags)})


@contract("EVEN-2", "static", "Even's wrapper reads EVEN_TERMINAL_PORT", ["launch"])
def check_even_port_env(ctx):
    pkg = even_package_dir(ctx.even)
    if not pkg:
        return even_missing("EVEN-2", ctx)
    hits = []
    for root, dirs, files in os.walk(pkg):
        dirs[:] = [d for d in dirs if d != "node_modules"]  # its own dependencies, not Even's code
        for name in files:
            if name.endswith((".js", ".mjs", ".cjs")) and scan_file(os.path.join(root, name), ["EVEN_TERMINAL_PORT"]):
                hits.append(os.path.relpath(os.path.join(root, name), pkg))
    return result("EVEN-2", OK if hits else FAIL,
                  [] if hits else ["no file in the even-terminal package mentions EVEN_TERMINAL_PORT"],
                  {"files": sorted(hits)})


# --- live -------------------------------------------------------------------------------------

KNOWN_STATUSES = {"idle", "busy", "waiting", "shell"}
SESSION_SPEC = {"pid": int, "procStart": str, "sessionId": str, "cwd": str, "status": str,
                "statusUpdatedAt": (int, float)}


def session_files(rc):
    """[(path, record)] for session files whose process still exists."""
    out = []
    for name in sorted(os.listdir(rc.SESSIONS_DIR)) if os.path.isdir(rc.SESSIONS_DIR) else []:
        if not name.endswith(".json"):
            continue
        path = os.path.join(rc.SESSIONS_DIR, name)
        try:
            with open(path) as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(record.get("pid"), int) and os.path.exists(f"/proc/{record['pid']}"):
            out.append((path, record))
    return out


@contract("SESS-1", "live", "live session files have the fields and types afield reads",
          ["live_processes", "record_live", "should_convert", "phone_lost"])
def check_session_files(ctx):
    files = session_files(ctx.rc)
    if not files:
        return result("SESS-1", SKIP, ["no running Claude Code session to look at"])
    problems, keys, statuses, links = [], None, set(), 0
    for path, record in files:
        problems += type_problems(record, SESSION_SPEC, os.path.basename(path) + ": ")
        keys = set(record) if keys is None else keys & set(record)  # in every file: optional ones vary
        statuses.add(str(record.get("status")))
        link = record.get("bridgeSessionId")
        if link is not None:
            if not (isinstance(link, str) and link.startswith("session_")):
                problems.append(f"{os.path.basename(path)}: bridgeSessionId {link!r} does not start with 'session_'")
            links += 1
    if not links:
        problems.append("no live session records a bridgeSessionId (its phone link)")
    unknown = sorted(statuses - KNOWN_STATUSES)
    detail = problems + [f"new status value {v!r} (afield acts only on 'idle')" for v in unknown]
    # bridgeSessionId is checked above; a session without a phone link (a glasses turn, a plain
    # `claude`) has none, so it is not compared with the snapshot (it flickered "changed").
    return result("SESS-1", FAIL if problems else CHANGED if unknown else OK, detail,
                  {"keys": sorted(keys - {"bridgeSessionId"})})


@contract("SESS-2", "live", "procStart in a session file is field 22 (starttime) of /proc/<pid>/stat",
          ["pid_is_current", "live_processes"])
def check_proc_start(ctx):
    files = session_files(ctx.rc)
    if not files:
        return result("SESS-2", SKIP, ["no running Claude Code session to look at"])
    bad = [f"{os.path.basename(p)}: procStart {r.get('procStart')!r} does not match /proc/{r['pid']}/stat"
           for p, r in files if not ctx.rc.pid_is_current(r["pid"], r.get("procStart"))]
    return result("SESS-2", FAIL if bad else OK, bad, None)


def tracked_live(ctx):
    """Open registry sessions that are running in an afield window and have a phone link. Only a
    window's --remote-control writes a bridge-session line, so anything else is left out: a phone
    chat still on the Remote Control server (TRANS-2 failed on every busy one, 2026-11-10) and a
    Claude Code background session (a branched chat paused afield for 42 h, 2026-11-25). Where it
    runs is read from the process tree, not from the registry, which can be out of date."""
    live = {r["sessionId"]: r for _p, r in session_files(ctx.rc)}
    try:
        windowed = {p["uuid"] for p in ctx.rc.live_processes(ctx.rc.pane_map(ctx.rc.tmux_panes()))
                    if p["host"] == "tmux"}
    except Exception:
        windowed = set()  # tmux unreadable: TMUX-1 reports that; a guess here could pause afield
    return [(e, live[u]) for u, e in ctx.rc.load().items()
            if e.get("state") == "open" and e.get("link") and u in live and u in windowed]


@contract("SESS-3", "live", "a Claude Code background session's file has kind \"bg\"; `claude stop` and `claude rm` "
          "stop and remove one", ["classify", "stop_process"], critical=False)
def check_background_sessions(ctx):
    # Not critical: if it breaks, a background session is just left where it is ("open elsewhere").
    problems = []
    for sub, words in (("stop", "background session"), ("rm", "background session")):
        text = ctx.help(sub)
        if f"claude {sub} <id>" not in text or words not in text:
            problems.append(f"`claude {sub} --help` no longer reads 'claude {sub} <id>' ... '{words}'")
    kinds = sorted({str(r.get("kind")) for _p, r in session_files(ctx.rc)})  # a new kind shows as "changed"
    return result("SESS-3", FAIL if problems else OK, problems, {"kinds": kinds})


@contract("TRANS-1", "live", "a transcript lives at projects/<cwd, non-alphanumerics as '-'>/<uuid>.jsonl",
          ["transcript_path", "keep_link", "has_conversation"])
def check_transcript_path(ctx):
    pairs = tracked_live(ctx)
    if not pairs:
        return result("TRANS-1", SKIP, ["no tracked session is running"])
    missing = [f"{e['uuid'][:8]}: no file at {ctx.rc.transcript_path(r.get('cwd'), e['uuid'])}"
               for e, r in pairs if not os.path.exists(ctx.rc.transcript_path(r.get("cwd"), e["uuid"]))]
    return result("TRANS-1", FAIL if len(missing) == len(pairs) else OK, missing, None)


def last_bridge_record(path):
    last = None
    with open(path, errors="replace") as fh:
        for line in fh:
            if '"bridge-session"' in line:
                try:
                    last = json.loads(line)
                except ValueError:
                    pass
    return last


@contract("TRANS-2", "live", "the last bridge-session line is cse_<id> for the phone link session_<id>",
          ["link_record", "keep_link", "last_link_in_transcript"])
def check_bridge_line(ctx):
    pairs = [(e, r) for e, r in tracked_live(ctx) if r.get("bridgeSessionId")]
    if not pairs:
        return result("TRANS-2", SKIP, ["no tracked session with a phone link is running"])
    problems, keys = [], set()
    for e, r in pairs:
        path = ctx.rc.transcript_path(r.get("cwd"), e["uuid"])
        try:
            record = last_bridge_record(path)
        except OSError:
            continue
        if record is None:
            problems.append(f"{e['uuid'][:8]}: transcript has no bridge-session line")
            continue
        keys |= set(record)
        problems += type_problems(record, {"type": str, "sessionId": str, "bridgeSessionId": str}, e["uuid"][:8] + ": ")
        link = r["bridgeSessionId"]
        want = "cse_" + link[len("session_"):]
        if ctx.rc.last_link_in_transcript(path) != want:
            problems.append(f"{e['uuid'][:8]}: last bridge-session line names {record.get('bridgeSessionId')!r}, "
                            f"the running session uses {link!r} (expected {want!r})")
    return result("TRANS-2", FAIL if problems else OK, problems, {"keys": sorted(keys)})


KNOWN_ENTRYPOINTS = {"cli", "sdk-cli", "sdk-ts"}


@contract("TRANS-3", "live", "transcripts start with entrypoint/cwd/timestamp; glasses chats are sdk-ts",
          ["recent_transcripts", "should_adopt"])
def check_entrypoints(ctx):
    found = ctx.rc.recent_transcripts(max_age=14 * 24 * 3600)
    if not found:
        return result("TRANS-3", SKIP, ["no transcript written in the last 14 days"])
    problems = [f"{t['uuid'][:8]}: no cwd on its entrypoint line" for t in found if not t.get("cwd")]
    seen = {t["entrypoint"] for t in found}
    detail = problems + [f"new entrypoint value {e!r} (glasses adoption only looks for 'sdk-ts')"
                         for e in sorted(seen - KNOWN_ENTRYPOINTS, key=str)]
    return result("TRANS-3", FAIL if problems else CHANGED if detail else OK, detail, None)


@contract("TRANS-4", "live", "transcripts are compact JSON: user lines contain '\"type\":\"user\"'",
          ["has_conversation", "first_prompt"])
def check_compact_json(ctx):
    pairs = tracked_live(ctx)
    for e, r in pairs:
        path = ctx.rc.transcript_path(r.get("cwd"), e["uuid"])
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") == "user":
                        ok = '"type":"user"' in line
                        return result("TRANS-4", OK if ok else FAIL,
                                      [] if ok else [f"{e['uuid'][:8]}: user lines are no longer compact JSON"], None)
        except OSError:
            continue
    return result("TRANS-4", SKIP, ["no tracked transcript with a user message"])


@contract("CRED-1", "live", "the OAuth token is at claudeAiOauth.accessToken in .credentials.json",
          ["api_request"])
def check_credentials(ctx):
    if not any(getattr(ctx.rc, k, True) for k in ("ARCHIVE_SYNC", "PHONE_TITLES", "RETIRE_ARCHIVES")):
        return turned_off("CRED-1", "archive_sync, phone_titles and retire_archives")  # never read the token
    try:
        with open(ctx.rc.CREDENTIALS) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as err:
        return result("CRED-1", FAIL, [f"cannot read {ctx.rc.CREDENTIALS}: {err.__class__.__name__}"])
    oauth = data.get("claudeAiOauth")
    ok = isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str) and oauth["accessToken"]
    return result("CRED-1", OK if ok else FAIL, [] if ok else ["no claudeAiOauth.accessToken"],
                  {"keys": sorted(oauth) if isinstance(oauth, dict) else []})  # names only, never values


@contract("AUTH-1", "live", "`claude auth status --json` says when the Claude login ends, and that is over 3 days away",
          ["read_login", "login_status", "login_words"], critical=False)
def check_login(ctx):
    # Not critical: nothing afield does depends on the date, but every session stops when the
    # login ends, so the warning goes out through the contract alerts.
    info = ctx.rc.read_login(ctx.claude)
    if not isinstance(info, dict):
        return result("AUTH-1", FAIL, ["`claude auth status --json` did not print JSON"])
    observed = {"keys": sorted(info)}  # names only: the values include your email
    if "loggedIn" not in info or (info.get("loggedIn") and "loginExpiresAt" not in info):
        return result("AUTH-1", FAIL, ["no loggedIn/loginExpiresAt in `claude auth status --json`"], observed)
    state, ends = ctx.rc.login_status(info, time.time())
    words = [f"login {ctx.rc.login_words(state, ends)}"]
    if state == "ok":
        return dict(result("AUTH-1", OK, [], observed), note=words)
    return result("AUTH-1", FAIL, words, observed)


KNOWN_API_STATUSES = {"idle", "running", "requires_action", "archived", "completed", "failed"}
LIST_ITEM_SPEC = {"id": str, "session_status": str, "updated_at": str, "title": str}


def api_problem(err):
    if isinstance(err, urllib.error.HTTPError):
        return FAIL, f"HTTP {err.code} {err.reason}"
    if isinstance(err, (urllib.error.URLError, TimeoutError, OSError)):
        return SKIP, f"network unavailable ({err.__class__.__name__})"
    return FAIL, f"{err.__class__.__name__}: {err}"


@contract("API-1", "live", "GET /v1/sessions lists phone chats with id, session_status, updated_at, title",
          ["fetch_statuses", "archived_to_retire"])
def check_api_list(ctx):
    if not getattr(ctx.rc, "ARCHIVE_SYNC", True):
        return turned_off("API-1", "archive_sync")
    try:
        data = ctx.rc.api_request("/v1/sessions?limit=5")
    except Exception as err:
        status, why = api_problem(err)
        return result("API-1", status, [why])
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return result("API-1", FAIL, ["the response has no 'data' list"], {"top": sorted(data) if isinstance(data, dict) else []})
    problems, keys, statuses = [], None, set()
    for item in items:
        problems += type_problems(item, LIST_ITEM_SPEC, f"{str(item.get('id'))[:16]}: ")
        if not str(item.get("id", "")).startswith("session_"):
            problems.append(f"id {item.get('id')!r} does not start with 'session_'")
        keys = set(item) if keys is None else keys & set(item)  # on every chat: some fields are optional
        statuses.add(str(item.get("session_status")))
    unknown = sorted(statuses - KNOWN_API_STATUSES)
    detail = problems + [f"new session_status value {v!r} (archive sync looks for 'archived')" for v in unknown]
    return result("API-1", FAIL if problems else CHANGED if unknown else OK, detail,
                  {"top": sorted(data), "item": sorted(keys or ())})


@contract("API-2", "live", "GET /v1/sessions/<link> returns the chat's title", ["fetch_title"])
def check_api_one(ctx):
    if not getattr(ctx.rc, "PHONE_TITLES", True):
        return turned_off("API-2", "phone_titles")
    pairs = tracked_live(ctx)
    if not pairs:
        return result("API-2", SKIP, ["no tracked session with a phone link is running"])
    link = pairs[0][0]["link"]
    try:
        data = ctx.rc.api_request("/v1/sessions/" + link, timeout=5)
    except Exception as err:
        status, why = api_problem(err)
        return result("API-2", status, [why])
    problems = type_problems(data, {"title": str, "session_status": str})
    return result("API-2", FAIL if problems else OK, problems, {"keys": sorted(data)})


EVEN_INSTANCE_SPEC = {"pid": int, "port": int, "startedAt": (int, float, str)}


EVEN_PROXY_NAMES = ["EVEN_HOST_MODE", "EVEN_HOST_INTERFACE", "/api/claude-sync/hook"]


@contract("EVEN-4", "static", "Even reads EVEN_HOST_MODE/EVEN_HOST_INTERFACE and its trusted hook is /api/claude-sync/hook",
          ["even-terminal.service", "even_network", "glasses_may_forward"], critical=False)
def check_even_host_env(ctx):
    pkg = even_package_dir(ctx.even)
    if not pkg:
        return even_missing("EVEN-4", ctx)
    found = set()
    for root, dirs, files in os.walk(pkg):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for name in files:
            if name.endswith((".js", ".mjs", ".cjs")):
                found |= scan_file(os.path.join(root, name), EVEN_PROXY_NAMES)
    missing = [n for n in EVEN_PROXY_NAMES if n not in found]
    problems = [f"no file in the even-terminal package mentions {n}" for n in missing]
    network = ctx.rc.even_network().get("mode")
    if network not in (None, "tailscale", "lan", "interface"):
        problems.append(f"~/.even-terminal/config.json network mode is {network!r}; the glasses proxy "
                        "supports tailscale, lan and interface")
    return result("EVEN-4", FAIL if problems else OK, problems, {"found": sorted(found)})


@contract("EVEN-5", "live", "GET /api/sessions returns {sessions: [{id, title, ...}]}",
          ["glasses_sessions", "glasses_list_request"], critical=False)
def check_even_sessions(ctx):
    rc = ctx.rc
    if not rc.even_up():
        return result("EVEN-5", SKIP, [f"nothing listens on port {rc.EVEN_PORT}"])
    try:
        with open(os.path.join(rc.HOME, ".even-terminal", "config.json")) as fh:
            token = json.load(fh).get("token")
    except (OSError, ValueError, AttributeError):
        token = None
    if not token:
        return result("EVEN-5", SKIP, ["no token in ~/.even-terminal/config.json"])
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", rc.EVEN_PORT, timeout=20)
        conn.request("GET", "/api/sessions?provider=claude&limit=3", headers={"Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        status, body = resp.status, resp.read()
        conn.close()
        doc = json.loads(body)
    except (OSError, ValueError) as err:
        return result("EVEN-5", FAIL, [f"GET /api/sessions failed: {err}"])
    problems, keys = [], set()
    if status != 200:
        problems.append(f"GET /api/sessions answered {status}")
    sessions = doc.get("sessions") if isinstance(doc, dict) else None
    if not isinstance(sessions, list):
        problems.append("the answer has no `sessions` list")
        sessions = []
    for item in sessions:
        keys |= set(item) if isinstance(item, dict) else set()
        problems += type_problems(item, {"id": str, "title": str}, "session: ") if isinstance(item, dict) else [
            "a session is not an object"]
    return result("EVEN-5", FAIL if problems else OK, sorted(set(problems)), {"keys": sorted(keys)})


@contract("EVEN-3", "live", "a running Even server writes ~/.even-terminal/instances/*.json with pid, port, startedAt",
          ["even_instance"])
def check_even_instance(ctx):
    rc = ctx.rc
    if not rc.even_up():
        return result("EVEN-3", SKIP, [f"nothing listens on port {rc.EVEN_PORT}"])
    paths = glob.glob(os.path.join(rc.HOME, ".even-terminal", "instances", "*.json"))
    problems, keys = [], set()
    for path in paths:
        try:
            with open(path) as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        keys |= set(record)
        if record.get("port") == rc.EVEN_PORT:
            problems += type_problems(record, EVEN_INSTANCE_SPEC, os.path.basename(path) + ": ")
    if rc.even_instance() is None:
        problems.append(f"Even listens on {rc.EVEN_PORT}, but no instance file names a live pid on that port")
    return result("EVEN-3", FAIL if problems else OK, problems, {"keys": sorted(keys)})


@contract("TMUX-1", "live", "tmux list-panes formats, @afield_uuid tags and batched capture-pane work",
          ["tmux_panes", "capture_screens", "windowed_uuids"])
def check_tmux(ctx):
    rc = ctx.rc
    if rc.tmux("has-session", "-t", rc.TMUX_SESSION).returncode != 0:
        return result("TMUX-1", SKIP, ["the afield tmux server is not running"])
    panes = rc.tmux_panes()
    problems = []
    if not panes or not all(w.startswith("@") and pid > 0 for w, _u, _d, pid, _n in panes):
        problems.append(f"list-panes rows look wrong: {panes[:2]!r}")
    tagged = [w for w, u, d, _p, _n in panes if u and not d]
    if tagged:
        pair = tagged[:2]
        batch = rc.capture_screens(pair)
        single = {w: rc.tmux("capture-pane", "-p", "-t", w).stdout for w in pair}
        if set(batch) != set(pair):
            problems.append("batched capture-pane did not return one screen per window")
        elif any(batch[w].strip().splitlines()[:3] != single[w].strip().splitlines()[:3] for w in pair):
            problems.append("batched capture-pane screens differ from single captures")
    version = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.strip()
    return result("TMUX-1", FAIL if problems else OK, problems, {"version": version})


@contract("SYSD-1", "live", "systemctl show prints Id blocks and NRestarts; the slice's cgroup has memory.current, "
          "memory.stat inactive_file and oom_kill",
          ["units_show", "invocations", "slice_file", "slice_memory", "working_set", "oom_kills",
           "record_server_restarts"])
def check_systemd(ctx):
    rc = ctx.rc
    units = [rc.SERVER_UNIT, rc.TMUX_UNIT]
    shown = rc.units_show(units, ["ActiveState", "InvocationID", "UnitFileState", "NRestarts"])
    problems = [f"systemctl show gave nothing for {u}" for u in units if u not in shown]
    if rc.SERVER_UNIT in shown and not shown[rc.SERVER_UNIT].get("NRestarts", "").isdigit():
        problems.append(f"{rc.SERVER_UNIT} has no numeric NRestarts (server restarts go unnoticed)")
    for u in units:
        if u in shown and shown[u].get("ActiveState") == "active" and not shown[u].get("InvocationID"):
            problems.append(f"{u} is active but has no InvocationID")
    memory = rc.slice_file("memory.current").strip()
    if not memory.isdigit():
        problems.append(f"{rc.SLICE} memory.current is unreadable ({memory[:40]!r})")
    if not any(line.startswith("inactive_file ") for line in rc.slice_file("memory.stat").splitlines()):
        problems.append(f"{rc.SLICE} memory.stat has no inactive_file line (file cache would count as use)")
    if "oom_kill " not in rc.slice_file("memory.events"):
        problems.append(f"{rc.SLICE} memory.events has no oom_kill line")
    return result("SYSD-1", FAIL if problems else OK, problems, None)


@contract("PROC-1", "live", "the server's children run in afield.service's cgroup", ["classify"])
def check_server_cgroup(ctx):
    rc = ctx.rc
    pid = rc.units_show([rc.SERVER_UNIT], ["MainPID"]).get(rc.SERVER_UNIT, {}).get("MainPID", "0")
    if not pid.isdigit() or pid == "0":
        return result("PROC-1", SKIP, [f"{rc.SERVER_UNIT} is not running"])
    ok = rc.SERVER_UNIT in rc.cgroup_of(int(pid))
    return result("PROC-1", OK if ok else FAIL,
                  [] if ok else [f"/proc/{pid}/cgroup does not name {rc.SERVER_UNIT}"], None)


# --- e2e (opt-in) -----------------------------------------------------------------------------

E2E_SOCKET = "afield-e2e"
E2E_TITLE = "afield contract test (safe to ignore)"


def _wait(predicate, seconds, step=1.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def _session_record(rc, uuid, need_link=True):
    for _p, r in session_files(rc):
        if r.get("sessionId") == uuid and (r.get("bridgeSessionId") or not need_link):
            return r
    return None


def run_e2e(rc, log=print):
    """E2E-1..3: a throwaway session on its own tmux server. Always cleans up after itself."""
    # A clean environment, like systemd gives the real sessions: run from inside a Claude session,
    # tmux would otherwise pass on CLAUDECODE, TMUX and the like to the test session.
    keep = {k: os.environ[k] for k in ("HOME", "PATH", "USER", "LANG", "SHELL") if k in os.environ}
    tm = ["env", "-i", *[f"{k}={v}" for k, v in keep.items()], "TERM=xterm-256color", "tmux", "-L", E2E_SOCKET]
    uuid = str(uuid_mod.uuid4())
    cwd = rc.SERVED_DIR
    out = {c: result(c, SKIP, ["an earlier e2e step failed"]) for c in ("E2E-1", "E2E-2", "E2E-3")}
    link = None
    try:
        log(f"e2e: starting session {uuid[:8]} in {cwd} on tmux -L {E2E_SOCKET}")
        # A placeholder window holds the tmux session open: when the test session is stopped its
        # window closes, and without another window the tmux server would exit with it.
        subprocess.run(tm + ["new-session", "-d", "-s", "e2e", "-x", "200", "-y", "50", "-n", "hold",
                             "sleep 3600"], check=True, capture_output=True)
        subprocess.run(tm + ["new-window", "-d", "-t", "e2e:", "-n", "first", "-c", cwd,
                             "exec " + shlex.join(rc.launch_command(uuid, False, new=True, title=E2E_TITLE))],
                       check=True, capture_output=True)
        first = _wait(lambda: _session_record(rc, uuid), 90)
        if not first:
            out["E2E-1"] = result("E2E-1", FAIL, ["no session file with a phone link within 90 s"])
            return list(out.values())
        link = first["bridgeSessionId"]
        log(f"e2e: phone link {link}; sending one short message")
        subprocess.run(tm + ["send-keys", "-t", "e2e:first", "Reply with the single word: ok", "Enter"],
                       capture_output=True)
        path = rc.transcript_path(cwd, uuid)
        talked = _wait(lambda: rc.has_conversation(cwd, uuid) and (_session_record(rc, uuid) or {}).get("status") == "idle"
                       and os.path.exists(path), 120, 2)
        if not talked:
            out["E2E-1"] = result("E2E-1", FAIL, ["the session did not answer and go idle within 120 s"])
            return list(out.values())
        bridge = rc.last_link_in_transcript(path)
        want = "cse_" + link[len("session_"):]
        out["E2E-1"] = result("E2E-1", OK if bridge == want else FAIL,
                              [] if bridge == want else [f"transcript names {bridge!r}, expected {want!r}"],
                              None)
        if bridge != want:
            return list(out.values())
        log("e2e: stopping it the way the supervisor does (SIGTERM) and resuming")
        os.kill(first["pid"], signal.SIGTERM)
        rc.wait_gone(first["pid"])
        started = subprocess.run(tm + ["new-window", "-d", "-t", "e2e:", "-n", "resumed", "-c", cwd,
                                       "exec " + shlex.join(rc.launch_command(uuid, False))], capture_output=True, text=True)
        if started.returncode != 0:
            out["E2E-2"] = result("E2E-2", FAIL, [f"could not open the resume window: {started.stderr.strip()[:200]}"])
            return list(out.values())
        second = _wait(lambda: (lambda r: r if r and r["pid"] != first["pid"] else None)(_session_record(rc, uuid)), 90)
        if not second:
            out["E2E-2"] = result("E2E-2", FAIL, ["the resumed session did not register a phone link within 90 s"])
            return list(out.values())
        same = second["bridgeSessionId"] == link
        out["E2E-2"] = result("E2E-2", OK if same else FAIL,
                              [] if same else [f"resume made a new link {second['bridgeSessionId']}, was {link}"], None)
        log("e2e: archiving the test chat on the phone")
        archived = rc.archive_link(link)
        status = None
        if archived:
            status = _wait(lambda: (rc.fetch_statuses() or {}).get(link, (None,))[0] == "archived" and "archived", 30, 3)
        out["E2E-3"] = result("E2E-3", OK if archived and status else FAIL,
                              [] if archived and status else
                              ["the archive call failed" if not archived else "the chat did not show as archived within 30 s"],
                              None)
        return list(out.values())
    finally:
        _cleanup_e2e(rc, tm, uuid, cwd, link, log)


def _cleanup_e2e(rc, tm, uuid, cwd, link, log):
    record = _session_record(rc, uuid, need_link=False)
    if record:
        try:
            os.kill(record["pid"], signal.SIGTERM)
            rc.wait_gone(record["pid"])
        except OSError:
            pass
    subprocess.run(tm + ["kill-server"], capture_output=True)
    if link and (rc.fetch_statuses() or {}).get(link, (None,))[0] != "archived":
        rc.archive_link(link)
    path = rc.transcript_path(cwd, uuid)
    for leftover in (path, path[:-len(".jsonl")]):
        if os.path.isdir(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
        elif os.path.exists(leftover):
            os.remove(leftover)
    log(f"e2e: cleaned up {uuid[:8]} (tmux server, process, transcript; chat archived)")


for _cid, _title, _used in (
        ("E2E-1", "a new --remote-control session gets a link and writes it to its transcript",
         ["launch_command", "link_record", "last_link_in_transcript"]),
        ("E2E-2", "resuming a stopped session with --remote-control keeps its phone link",
         ["launch_command", "convert", "stop_process"]),
        ("E2E-3", "POST /v1/code/sessions/cse_<id>/archive archives the chat", ["archive_link", "cmd_retire"])):
    CONTRACTS.append(dict(id=_cid, level="e2e", title=_title, used_by=_used, critical=True, fn=None))


# --- running ----------------------------------------------------------------------------------


def load_snapshot(path=SNAPSHOT):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def run(rc, levels=("static", "live"), claude=None, log=print):
    """Runs the contracts at these levels. Returns (results, versions)."""
    ctx = Ctx(rc, claude)
    results = []
    for c in CONTRACTS:
        if c["level"] not in levels or c["fn"] is None:
            continue
        try:
            results.append(c["fn"](ctx))
        except Exception as err:  # a check that crashes is itself a broken assumption
            results.append(result(c["id"], FAIL, [f"the check crashed: {err.__class__.__name__}: {err}"]))
    if "e2e" in levels:
        results += run_e2e(rc, log)
    compare_to_snapshot(results, load_snapshot())
    v = versions(rc, claude)
    v["tmux"] = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.strip().replace("tmux ", "")
    return results, v


def snapshot_from(results, versions_seen):
    observed = {r["id"]: r["observed"] for r in results if r.get("observed") is not None and r["status"] in (OK, CHANGED)}
    return {"versions": versions_seen, "observed": observed}
