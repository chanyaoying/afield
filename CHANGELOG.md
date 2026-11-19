# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org) from v0.1.0.

## Unreleased

## v0.1.0 (2026-11-19)

First release. Changes since the first import (2026-11-09):

- Changed: `afield retire` no longer archives the chat in the app unless `retire_archives` is on.
  With `archive_sync`, `phone_titles` and `retire_archives` all off (the default), afield never
  reads your Claude login token; the README and SECURITY.md say what turning them on means.
- Changed: the `/retire` command is now `/afield-retire`, so it cannot clash with another tool's.
  `install.sh` removes the old link.
- Changed: Python 3.10 or newer (3.8 and 3.9 are past their end of life; CI could no longer get 3.8).
- Fixed: `afield new` refuses a title starting with `-`, which claude would have read as a flag.
- Security: the state folder is made private (700); afield warns about a secret file others can
  read; the glasses proxy refuses bodies over 1 MB and more than 64 requests at once. SECURITY.md
  now lists everything afield does to your machine and what it sends out.
- Install: the units find `python3` and `tmux` on PATH instead of in /usr/bin; `install.sh` checks
  for tmux, Python 3.10+, a systemd user manager, lingering and the cgroup memory controller.

- Added: contract alerts. With `alerts_url` set, each new contract failure, and the all-clear
  after it, is sent once to your webhook: `alerts_format` json (POST {source, title, text},
  HMAC-signed when `alerts_secret_file` exists), ntfy, slack or discord. Failed sends retry every
  10 minutes. `afield alert test` sends a test message. `afield config` hides webhook paths.

- Fixed: messages from the glasses were lost in windows showing Claude Code's new "Teach auto mode
  about your environment?" pop-up. The glasses type the message and press Enter; the Enter answered
  the pop-up (or started its setup) instead. afield now answers "Don't show again" (which Claude
  Code saves for every session) and closes the setup screen with Esc, recognising the pop-up only
  by whole lines at the bottom of a window. New contract SCREEN-2 checks the pop-up's text and
  option order in the claude binary; it reports but pauses nothing.

- Fixed: a window could stay off the phone for good while `afield list` looked fine. The server
  restart that unties a moved chat ran 5 s after its window started, while it took its first
  phone message, and the window stood down (4090). The notice scrolled off screen, so it was never
  relaunched. Now the restart waits until each moved window has been up a minute, holds its link
  and is idle; a window whose session file has lost its phone link is relaunched (at most every
  5 minutes); and `afield list` shows `PHONE DISCONNECTED`.
- Fixed: a chat started on the phone could run as two copies at once, its window and a copy on
  the server, which split your messages between them and edited the same files. The cloud gives
  such a chat's phone messages to the server that created it for as long as that server runs. The
  supervisor now restarts the server once it is empty after moving a chat into a window, which
  unties the chat. Until then, a window still working when a server copy takes over is stopped
  (Esc). SCREEN-1 now also checks "esc to interrupt"; SESS-1 knows the `shell` status.
- The glasses list only the sessions afield keeps open, under their phone titles: `afield even-proxy`
  (`afield-even-proxy.service`) takes the glasses' address and Even Terminal stays on 127.0.0.1.
  Config `glasses_only_afield` (on by default). New contracts EVEN-4 and EVEN-5.
- `afield doctor` no longer crashes on a fresh install with no state folder yet.
- Glasses sessions wait at most 90 s for a broken Even Terminal, then start without it (they used
  to wait forever). `afield status` warns when Even restarts 3 times in an hour, and `afield doctor`
  fails EVEN-1/EVEN-2 when its unit is enabled but `even-terminal` cannot be found.
- `install.sh` finds `claude` and `even-terminal` in the usual install folders even under
  `systemd-run`, and stops before changing anything if either is missing.
- TRANS-2 no longer fails on a busy phone chat still hosted by the server; SESS-1 no longer flickers
  "changed" while a session without a phone link runs.
- Renamed from claude-rc to afield: the command, units (`afield.target` and friends), tmux
  socket, config (`~/.config/afield`), state (`~/.local/state/afield`) and environment variables
  (`AFIELD_*`). `install.sh` migrates an existing claude-rc install.
- MIT licence.
- Config file `~/.config/afield/config.json` (`afield config` shows the settings in use):
  served folder, Even port, session and memory caps, idle time. `install.sh` passes the ones systemd
  needs to the units as drop-ins.
- The undocumented sessions API is opt-in: `archive_sync` and `phone_titles` are off by default, and
  their contracts (API-1, API-2) are skipped while off.
- No folder's trust prompt is answered unless it is listed in `auto_trust_dirs` (it used to be `~`).
- Units contain no personal paths (`%h`, `/usr/bin/env`); PATH comes from where `install.sh` finds
  `claude` and `even-terminal`. The Tailscale wait is skipped when Tailscale is not installed.
- Remote Control server health: automatic restarts are logged, and `afield status` warns at 3 in
  an hour (the phone may not be able to start new chats).
- `afield status --json`; `afield list` shows each session's memory.
- The 5 GB reopen limit counts the slice's working set, not reclaimable file cache (which was about half
  of `memory.current`, enough to hold back reopens for no reason).
- `uninstall.sh`; CI (tests on every push, nightly static contracts on the newest Claude Code);
  SECURITY.md, CONTRIBUTING.md, issue templates.

## 2026-11-09 (before versioning)

- Contract tests (`afield doctor`, CONTRACTS.md): every Claude Code, Even, tmux and systemd internal
  afield relies on, checked at start, on version changes and every 6 h; a failed critical contract
  pauses moves, adoption, archive sync and link writes.
- Supervisor tick 57% cheaper: batched systemctl and tmux calls, registry written only on change.
- Even Terminal optional; everything under `afield.target`; 5 s idle everywhere; sessions go back
  on the glasses once Even returns.
- `install.sh` enables instead of re-enabling (disabling a linked unit deletes it).
- CLI: `list --all`, `status`, `logs`, `open --revive`, `help`; says when it waits on the supervisor.
- First import: supervisor, units, tests, `/retire`, skill and README.
