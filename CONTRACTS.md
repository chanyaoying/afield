# Contracts: what afield assumes about the software it drives

afield relies on behaviour nobody promised to keep stable: file layouts and field names inside
Claude Code, its command-line flags, text it shows on screen, a beta API, and Even Terminal's
wrapper. Each assumption below is a **contract** with an ID. `afield doctor` checks every one of
them against the real system and says exactly what differs when one breaks.

Last verified: 2026-11-19 on Claude Code 2.1.328, even-terminal 0.10.5, tmux 3.6, systemd 259.

## Read this first (for a Claude session or agent picking this up)

- **The checks:** `lib/afield_contracts.py`, one function per contract, named in each section below.
- **Their unit tests:** `tests/test_contracts.py`. Each check is fed a deliberately broken fake
  to prove it notices the change it guards against.
- **The baseline:** `contracts/snapshot.json` holds the shapes seen on the last good versions:
  key sets, flag lists, found strings. A result is `changed` when it still meets every
  requirement but differs from the baseline; the report prints the difference.
- **In the code:** every place that relies on a contract carries a `# contract: ID` comment.
  `grep -n "contract: SESS-1" bin/afield systemd/*` finds them. The unit tests fail if a
  contract has no such marker, or a marker names an unknown contract.

### Running it

```
afield doctor                     static + live checks (about 2 s, read-only)
afield doctor --e2e               also the end-to-end checks (about 1 min; creates one chat on
                                     the phone, sends it one message, archives it, removes it)
afield doctor --candidate PATH    static checks against a claude binary you have not switched to,
                                     e.g. ~/.local/share/claude/versions/2.1.320
afield doctor --update-snapshot   accept today's shapes as the new baseline (commit the file)
```

Exit status 1 means a contract failed. `afield status` shows the last result on its `contracts` line.

### What the supervisor does on its own

It runs the static and live checks:
- when it starts
- when the `claude` or `even-terminal` version changes
- every 6 hours
- every 10 minutes while some checks could only be skipped, e.g. right after boot

Results go to `~/.local/state/afield/contracts.json`, and failures go to its log. With `alerts_url`
set (see "Alerts" in the README), each new failure is also sent there once,
and again when everything passes; a send that fails is retried every 10 minutes. `afield alert test`
checks the path.

**When a critical contract fails, these pause:**
- moving phone chats into windows
- adopting glasses chats
- archive sync
- writing `bridge-session` lines into transcripts

**These carry on:**
- keeping sessions running
- reopening them
- relaunching windows

The pause lifts by itself once the checks pass again, for example after a fix and a supervisor
restart, or after `afield doctor`.

### Results

| Result | Meaning |
|---|---|
| `ok` | requirement holds and the shape matches the baseline |
| `changed` | requirement holds, but something differs from the baseline (the diff says what); nothing pauses |
| `FAIL` | the requirement no longer holds; afield may misbehave; risky actions pause |
| `skip` | nothing to check against right now (no running session, Even not installed, offline) |

### When a contract fails or changes: the playbook

1. Run `afield doctor` and read the failing line. It names the contract, what differs, and the
   afield functions that rely on it.
2. Read that contract's section below: why afield needs it and where to look.
3. Find the new behaviour.
   - **Field and flag renames:** compare the old and new binaries, e.g.
     `grep -a -o 'procStart[A-Za-z]*' ~/.local/share/claude/versions/<v>/... | sort -u`, and
     `claude --help`.
   - **File shapes:** look at a live file in `~/.claude/sessions/` or a transcript.
4. Fix afield, not the check, unless the check itself was wrong. Keep the decision in the
   pure functions at the top of `bin/afield`, with a unit test.
5. If the new behaviour also changes what the check should require, update the check in
   `lib/afield_contracts.py` and its unit test.
6. Run the tests:
   ```
   python3 -I tests/test_afield.py
   python3 -I tests/test_contracts.py
   afield doctor
   ```
   Run `afield doctor --e2e` too if the change touches links, resume or archiving.
7. Run `afield doctor --update-snapshot`, then update "Last verified" above and the section's
   notes, and commit.
8. Run `systemctl --user restart afield-supervisor`. Running sessions are not touched.

### Adding a contract

When afield starts relying on something new:
1. Add a check with `@contract(...)` in `lib/afield_contracts.py`.
2. Add a fake that breaks it, in `tests/test_contracts.py`.
3. Add a `### ID` section here.
4. Add a `# contract: ID` marker where the code relies on it.

The unit tests enforce all of these except the fake.

## Catalogue

| ID | Level | Assumption |
|---|---|---|
| CLI-1 | static | `claude` takes `--resume`, `--session-id`, `--remote-control` |
| CLI-2 | static | `claude remote-control` takes `--no-create-session-in-dir` |
| BIN-1 | static | the binary still uses the field and record names afield reads |
| SCREEN-1 | static | the TUI still shows the text afield matches on screen |
| SCREEN-2 | static | the auto mode setup pop-up's text and option order (afield answers "Don't show again") |
| EVEN-1 | static | `even-terminal` takes its unit's flags and has `even-terminal claude` |
| EVEN-2 | static | Even's wrapper reads `EVEN_TERMINAL_PORT` |
| SESS-1 | live | session files have the fields and types afield reads |
| SESS-2 | live | `procStart` equals the process start time in `/proc/<pid>/stat` |
| TRANS-1 | live | where a transcript file lives |
| TRANS-2 | live | the last `bridge-session` line picks the phone link |
| TRANS-3 | live | transcripts record their entrypoint; glasses chats are `sdk-ts` |
| TRANS-4 | live | transcripts are compact JSON |
| CRED-1 | live | where the OAuth token is |
| API-1 | live | the shape of `GET /v1/sessions` |
| API-2 | live | the shape of `GET /v1/sessions/<link>` |
| EVEN-3 | live | Even's instance file |
| TMUX-1 | live | tmux formats, tags and batched captures |
| SYSD-1 | live | `systemctl show` output and the slice's cgroup files |
| PROC-1 | live | server children run in `afield.service`'s cgroup |
| E2E-1 | e2e | a new Remote Control session gets a link and records it |
| E2E-2 | e2e | resuming keeps the phone link |
| E2E-3 | e2e | the archive endpoint archives a chat |

## Claude Code command line

### CLI-1

**Assumption:** `claude --help` lists `--resume`, `--session-id` and `--remote-control`.
`launch_command()` starts every session as `claude --resume <uuid> --remote-control`; new ones as
`claude --session-id <uuid> --remote-control [title]`.
**If it breaks:** windows open and exit at once; sessions go `failed` after 3 attempts.
**Check:** `check_cli_flags`. The baseline holds the full flag list, so a rename shows as `+new -old`.
**Never use** `--continue` or `--name` (see README, "Verified behaviour").

### CLI-2

**Assumption:** `claude remote-control --help` lists `--[no-]create-session-in-dir`.
`afield.service` runs the server with `--no-create-session-in-dir`, because a server-owned
session is reattached on restart, and that reattach rewrote a converted session's transcript.
**If it breaks:** the server unit fails to start; phone-started chats stop working.
**Check:** `check_server_flags`.

## Inside the claude binary

These checks only search the installed `claude` program for text, as `grep` would. afield never
modifies, patches or redistributes Claude Code.

### BIN-1

**Assumption:** the binary still contains these names:
- `bridgeSessionId`, `procStart`, `statusUpdatedAt`, `sessionId` (session-file fields)
- `bridge-session` (the transcript record type)
- `sdk-ts` (the glasses entrypoint)
- `ccr-byoc-2025-07-29` (the API beta header)
- `/v1/sessions`

This catches a rename in the version you are about to install, before any file is written in the
new shape: run it with `--candidate`.
**If it breaks:** see the matching live contract (SESS-1, TRANS-2, TRANS-3, API-1).
**Check:** `check_binary_names`. It scans the file in chunks, so it is cheap.

### SCREEN-1

**Assumption:** the TUI shows:
- "Yes, I trust this folder" and "Accessing workspace:" (the trust prompt that `trust_prompt_dir()`
  and `answer_trust()` answer for `~` only)
- "standing down" and "remote-control is active" (`stood_down()` relaunches a window whose Remote
  Control gave up, code 4090)
- `❯` (the selection marker)
- "esc to interrupt" (Esc stops a reply: `stop_ghost_replies()` presses it in a window that is
  still working after a server copy of its session took the phone connection)

**If it breaks:** new windows in `~` wait at the trust prompt, a stood-down window stays
disconnected from the phone, or a stood-down window finishes its reply alongside the server copy.
**Check:** `check_screen_text`.

### SCREEN-2

**Assumption:** Claude Code's auto mode setup pop-up looks like this, at the bottom of the window:
- the title "Teach auto mode about your environment?" on a line of its own
- three options, in this order: "1. Yes" (accept), "2. Not now" (later: asks again in a week),
  "3. Don't show again" (dismiss: saved in `~/.claude.json` as `autoModeEnvSetup.dismissed`, so no
  session shows it again)
- a footer ending "Esc to cancel"
- after Yes, a setup screen with "Also scan shell history" that Esc closes

Why afield cares: the glasses send a message by typing it and pressing Enter. While the pop-up is
open, that Enter answers the pop-up (Yes, the first option, starts the setup) and the message never
reaches the chat (2026-11-19, five windows). `env_setup_prompt()` recognises the pop-up only by
whole lines at the bottom of the screen; `dismiss_env_setup()` presses Down until `❯` is on
"Don't show again" (at most three times), and only then presses Enter (otherwise Esc). The setup screen gets Esc.
**If it breaks:** a reworded pop-up is no longer recognised and stays open, so glasses messages
are lost again; a reordered one gets Esc instead of "Don't show again" and comes back weekly.
**Not critical:** a failure is reported but pauses nothing, since afield presses keys only on an
exact match.
**Check:** `check_env_setup_text`. It also matches the options' labels and values in the binary,
so a reorder fails.

## Session files (`~/.claude/sessions/<pid>.json`)

### SESS-1

**Assumption:** each running Claude Code process has a file with:
- `pid` (int), `procStart` (str), `sessionId` (str), `cwd` (str)
- `status` (str: `idle`, `busy`, `waiting`, `shell`; afield treats everything but `idle` as working)
- `statusUpdatedAt` (ms, number)
- once Remote Control is connected, `bridgeSessionId` (`session_<id>`, the phone link). It is
  gone again after the window stands down (code 4090): `phone_lost()` relaunches an idle window
  whose session has a link but whose file has none.

`live_processes()` builds everything the supervisor knows about running sessions from these files.
Moving and adopting only act on `idle` sessions that have been idle for 5 s.
**If it breaks:** the supervisor sees no sessions, so it reopens duplicates or moves nothing.
**Check:** `check_session_files`. A new `status` value shows as `changed`. `bridgeSessionId` is
checked for its format but left out of the snapshot's key list: sessions without a phone link (a
glasses turn, a plain `claude`) have none.

### SESS-2

**Assumption:** `procStart` equals field 22 (starttime) of `/proc/<pid>/stat`. `pid_is_current()`
uses it to ignore stale files whose PID was reused.
**If it breaks:** every session looks dead, and the supervisor tries to reopen all of them.
**Check:** `check_proc_start`.

## Transcripts (`~/.claude/projects/<encoded cwd>/<uuid>.jsonl`)

### TRANS-1

**Assumption:** a session's transcript is `~/.claude/projects/<cwd with every non-alphanumeric
character replaced by '-'>/<uuid>.jsonl` (`transcript_path()`).
**If it breaks:** link keeping, reopen and `has_conversation()` can't find transcripts; sessions get
marked `closed`.
**Check:** `check_transcript_path`. It fails only if *no* running tracked session's transcript is
found, since a brand-new session may not have one yet.

### TRANS-2

**Assumption:** when Claude Code resumes a session with `--remote-control`, it reuses the phone link
named by the **last** `{"type":"bridge-session","sessionId":…,"bridgeSessionId":"cse_<id>"}` line
in the transcript. That `cse_<id>` corresponds to the link `session_<id>` in the session file.

`keep_link()` appends such a line (`link_record()`) before relaunching, so a moved or reopened
session keeps its phone entry. This is the only transcript write afield makes. Claude
Code's own lines also carry `lastSequenceNum` and the account and organization ids; afield
writes only the three fields above, and resume accepts that.
**If it breaks:** moved and reopened sessions appear on the phone as *new* chats, and the old
entries go stale.
**Check:** `check_bridge_line`, against every running tracked session in a window (a phone chat
still hosted by the Remote Control server has no bridge-session line until it moves). E2E-1 and E2E-2 test the
behaviour end to end.

### TRANS-3

**Assumption:** a transcript's first lines carry `entrypoint`, `cwd` and `timestamp`. Seen values:
- `cli` (interactive)
- `sdk-cli`
- `sdk-ts` (Agent SDK, used by Even Terminal for glasses-started chats)

`recent_transcripts()` reads these, and `should_adopt()` adopts only `sdk-ts` chats into windows.
**If it breaks:** glasses-started chats stay off the terminal and phone.
**Check:** `check_entrypoints` (last 14 days). A new entrypoint value shows as `changed`.

### TRANS-4

**Assumption:** transcripts are compact JSON, so a user message line contains `"type":"user"`
with no spaces. `has_conversation()` and `first_prompt()` use that substring as a fast filter.
**If it breaks:** every transcript looks empty, sessions are marked `closed`, and `list --all`
shows no first prompts.
**Check:** `check_compact_json`.

## Credentials and the sessions API (beta)

Archive sync and phone titles use this API only when `archive_sync` / `phone_titles` are on in the
config (both off by default). While one is off, its check (API-1 / API-2) is skipped with
"not used: … is off in the config", and that skip is not retried every 10 minutes like others.

### CRED-1

**Assumption:** the OAuth token is at `claudeAiOauth.accessToken` in `~/.claude/.credentials.json`.
`api_request()` sends it as a Bearer token. The check never prints the token, only key names.
**If it breaks:** titles stop updating and archive sync stops; everything else works.
**Check:** `check_credentials`.

### API-1

**Assumption:** `GET https://api.anthropic.com/v1/sessions?limit=N` with headers
`anthropic-version: 2023-06-01` and `anthropic-beta: ccr-byoc-2025-07-29` returns
`{"data": [...]}`. Each item has:
- `id` (`session_…`)
- `session_status` (`idle`, `running`, `archived`, …)
- `updated_at` (ISO time)
- `title`

`fetch_statuses()` feeds archive sync (`archived_to_retire()`): a running chat you archive in the
app is retired.
**If it breaks:** archive sync stops; you archive in the app instead. Failures are swallowed, so
nothing else breaks.
**Check:** `check_api_list` (GET only). HTTP errors fail; being offline is a skip.

### API-2

**Assumption:** `GET /v1/sessions/<session_id>` returns the chat with `title` and `session_status`.
`fetch_title()` renames tmux windows after the phone's titles.
**If it breaks:** window names stop following the phone.
**Check:** `check_api_one`.

## Even Terminal (glasses; optional)

### EVEN-1

**Assumption:** `even-terminal --help` lists `--log-file`, `--log-level`, `--claude-allowed-tools`
and `--port`, plus the `even-terminal claude` command. `even-terminal.service` uses the flags;
`launch_command()` wraps sessions as `even-terminal claude --resume <uuid> --remote-control`.
**If it breaks:** the Even unit fails, or wrapped sessions exit at once. After 3 failed attempts a
session starts unwrapped.
**Check:** `check_even_flags`. Skipped when Even is not installed. If `even-terminal.service` is
enabled but `even-terminal` is not on afield's PATH, it fails: the unit is then crash-looping (rerun
`install.sh`, or `install.sh --without-even`).

### EVEN-2

**Assumption:** Even's wrapper reads `EVEN_TERMINAL_PORT` to find its server. `launch()` sets it to
3456 for every window.
**If it breaks:** wrapped sessions can't reach the glasses server.
**Check:** `check_even_port_env` (searches the package's own source files). Fails like EVEN-1 when
the unit is enabled but the binary is not found.

### EVEN-3

**Assumption:** a running Even server writes `~/.even-terminal/instances/<…>.json` with `pid`,
`port` and `startedAt`. `even_instance()` turns these into an identity; when it changes (Even
restarted), sessions are reopened, because every wrapper exits with its server.
**If it breaks:** after an Even restart, sessions aren't reopened on the glasses, or Even counts as
down.
**Check:** `check_even_instance`. Skipped when nothing listens on 3456.

### EVEN-4

**Assumption:** Even reads `EVEN_HOST_MODE` and `EVEN_HOST_INTERFACE` before its config file, so the
unit can keep it on 127.0.0.1 (`interface`, `lo`) while `afield even-proxy` takes the address the
glasses use (from `network` in `~/.even-terminal/config.json`: tailscale, lan or interface). Its
one unauthenticated, loopback-trusted route is `/api/claude-sync/hook`, which the proxy never passes
on (nor any WebSocket upgrade: Even trusts wrapper connections from 127.0.0.1).
**If it breaks:** Even listens on the glasses' address itself and the proxy cannot start (the
glasses then list every session again), or a renamed trusted route could be reached through the proxy.
**Check:** `check_even_host_env` (searches the package's source). Pauses nothing.

### EVEN-5

**Assumption:** `GET /api/sessions?provider=claude` answers `{"sessions": [{"id", "title", …}]}`,
newest first, and takes `limit`. `afield even-proxy` asks for 50 and keeps the sessions afield keeps
open, under their phone titles.
**If it breaks:** the proxy passes the answer on unchanged; the glasses list everything again.
**Check:** `check_even_sessions`, against the running Even with its token. Pauses nothing.

## tmux, systemd and Linux

### TMUX-1

**Assumption:** on tmux 3.x:
- `list-panes -s -F '#{window_id}\t#{@afield_uuid}\t#{pane_dead}\t#{pane_pid}\t#{window_name}'`
  returns one row per pane, with the `@afield_uuid` user option afield sets on its windows.
- `capture-pane -p` calls joined with `\;` and `display-message -p <separator>` print every screen
  in one call (`capture_screens()`).

**If it breaks:** the supervisor loses track of windows, or stood-down and trust-prompt detection
stops.
**Check:** `check_tmux` (compares batched with single captures).

### SYSD-1

**Assumption:** `systemctl --user show A B -p Id -p …` prints one blank-line-separated block per
unit (`parse_show()`), active units have an `InvocationID`, and the server has a numeric
`NRestarts` (its automatic restarts, for the crash-loop warning in `afield status`). The slice's
cgroup directory has `memory.current`, a `memory.stat` with an `inactive_file` line (subtracted, so
reclaimable file cache does not count against `defer_above_gb`) and a `memory.events` with an
`oom_kill` line.
**If it breaks:** restarts of the server or tmux go unnoticed; the memory limits on reopening stop
working, or count file cache as use.
**Check:** `check_systemd`.

### PROC-1

**Assumption:** the Remote Control server's process, and so its children (phone-started chats),
run in a cgroup whose path names `afield.service`. `classify()` uses this to tell server
children from other processes.
**If it breaks:** phone-started chats are never moved into windows.
**Check:** `check_server_cgroup`.

## End to end (on request only: `afield doctor --e2e`)

These start a throwaway session on its own tmux server (`-L afield-e2e`) in `~/afield`, using
afield's own `launch_command()`. Each run:
- shows one chat on the phone, titled "afield contract test (safe to ignore)"
- sends it one short message (a few tokens)

It always cleans up: the process is stopped, the chat archived, and the transcript and tmux server
removed. The supervisor ignores the session, since it runs outside afield's tmux and server.
An e2e failure is reported but never pauses the supervisor, since these checks only run when you
ask. Before acting on one, rule out the test itself. The first run on 2026-11-09 failed E2E-2
because the test's tmux session closed with its only window; a placeholder window now holds it open.

### E2E-1

**Assumption:** `claude --session-id <uuid> --remote-control <title>` registers a phone link in its
session file. After one message, the transcript's last `bridge-session` line names that link as
`cse_<id>`.
**If it breaks:** see TRANS-2: new sessions can't be kept on their links.

### E2E-2

**Assumption:** stopping a session with SIGTERM (as `stop_process()` does) and starting
`claude --resume <uuid> --remote-control` brings it back on the **same** phone link. This is the
behaviour every move, reopen and relaunch depends on.
**If it breaks:** every relaunch creates a new phone chat. This is the most serious failure.

### E2E-3

**Assumption:** `POST /v1/code/sessions/cse_<id>/archive` (body `{}`) archives the chat, and the
list then shows it as `archived`. `archive_link()` is used by `afield retire`.
**If it breaks:** `retire` stops archiving on the phone; archive by hand in the app.
