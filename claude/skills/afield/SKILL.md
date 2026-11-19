---
name: afield
description: Use when the user asks about afield, their sessions across terminal/phone/glasses, Remote Control sessions on this machine, tmux windows of sessions, adopting/reviving/retiring/reopening a session, or wants to change the afield supervisor, its units or tests.
---

# afield

You are probably running inside afield right now. Every session on this machine is one interactive
`even-terminal claude --resume <uuid> --remote-control` in a tmux window on server `-L afield`,
session `rc`. The same process serves the SSH terminal, the phone (Remote Control) and the Even G2
glasses. The supervisor (`afield-supervisor.service`, 5 s tick; everything grouped
under `afield.target`) keeps them running.

The repository is the folder two levels above `readlink -f ~/.local/bin/afield` (below: `<repo>`).
Full documentation: `<repo>/README.md`. Read it before changing anything.
Everything afield assumes about Claude Code, Even, tmux and systemd internals is catalogued by ID
in `<repo>/CONTRACTS.md`. If `afield doctor` or `afield status` reports a
failed or changed contract, follow the playbook there.

## Answering "how do I…"

| Want | Command |
|---|---|
| see all sessions | `afield list` (`--all` adds forgotten/untracked chats) |
| health check | `afield status` (`--json` for scripts) |
| settings in use / change one | `afield config` / `afield config set <key> <value>` (`unset <key>`; `afield config help` says what each change needs) |
| attach / one session | `afield` / `afield open <name\|id>` |
| new session with a phone link | `afield new [title]` |
| bring back a closed/failed one | `afield reopen <name\|id>` |
| bring back a forgotten/untracked one | `afield open --revive <id>` (or `reopen --revive`) |
| move in a session from a plain terminal | `afield adopt <id>` (or `adopt --revive <id>` if stopped) |
| stop tracking | `afield forget <name\|id>` |
| archive + forget + close | `afield retire [name\|id]` or `/afield-retire` |
| logs | `afield logs` |
| is everything we rely on in Claude Code / Even still the same? | `afield doctor` (`--e2e`, `--candidate <binary>`) |

Phone- and glasses-started sessions move into a window on their own once they've been idle for 5 s.
Even Terminal (the glasses) is optional: `install.sh --with-even` / `--without-even`. The glasses' session list goes through `afield-even-proxy.service` (only afield's sessions, phone titles; config `glasses_only_afield`); Even itself listens on 127.0.0.1 only. Archiving a *running* chat in the app retires it within a minute when config `archive_sync` is on (`afield config` shows it).
Settings (served folder, caps, idle time, the opt-in phone API, auto-trusted folders) live in
`~/.config/afield/config.json`; `install.sh` turns the ones systemd needs into unit drop-ins.

## Rules when changing the system

- The source of truth is `<repo>` (a git checkout). The live
  paths (`~/.local/bin/afield`, the units in `~/.config/systemd/user`, `/afield-retire`, this skill) are
  symlinks into it, so edit the repo file and commit. Don't make `*.bak-*` copies. Never `sed -i` or
  `mv` over a live path: that replaces the symlink with a plain file and the repo falls out of step
  (rerun `install.sh` to repair it). Run `git status` first; another session may have uncommitted work.
- Run `python3 -I tests/test_afield.py`, `python3 -I tests/test_contracts.py` and `afield doctor`
  before and after. New decisions go in the pure functions at the top of `bin/afield`, with a test.
- Relying on a new internal (a field, flag, file, on-screen text, API)? Add a contract: a check in
  `lib/afield_contracts.py`, a broken-fake test, a `### ID` section in CONTRACTS.md, and a
  `# contract: ID` marker where the code relies on it (the tests enforce the last three).
- Never fix a failing contract by loosening its check unless the check itself was wrong. Fix
  afield, then `afield doctor --update-snapshot` and commit.
- Touch `registry.json` only under `flock ~/.local/state/afield/registry.lock`, or better, through a
  `afield` command.
- The only transcript write afield makes is appending a `bridge-session` line, which `keep_link`
  does. Never rewrite transcripts.
- Never start `claude` with `--continue` or `--name`, or as a `--bg` job. Never add a server-owned
  session (keep `--no-create-session-in-dir`). See "Verified behaviour" in the README for why.
- After editing the script, `systemctl --user restart afield-supervisor`. This doesn't touch running
  sessions. Restarting `afield-tmux` or `even-terminal` *does* restart every session; ask first.
- Archiving or retiring is outward-facing (it changes the phone). Do it only when the user asks.
