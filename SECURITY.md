# Security

afield keeps Claude Code sessions running on your machine and reachable from the Claude app and,
optionally, from Even G2 glasses. Whoever can send a message to those sessions can make Claude act on
your machine with the permissions you gave it. Treat access to your Claude account, and to the Even
Terminal server, as access to the machine.

What afield itself does to your machine and accounts, all as your user (systemd user services;
nothing needs root):

**Your Claude login.** afield reads your Claude Code OAuth token (`~/.claude/.credentials.json`)
only when `archive_sync`, `phone_titles` or `retire_archives` is on, to call Anthropic's
undocumented sessions API. All three are off by default; then afield never opens that file. It
never logs or prints the token. Using it from a tool other than Claude Code may not be allowed by
the terms for your account: at your own risk (see the README).

**Your transcripts.** It appends one `bridge-session` line to a session's transcript when it moves
a phone chat into a window, so the window reuses the chat's phone link. It never changes or
removes existing lines.

**Keys it presses in your sessions** (tmux `send-keys`, only on exact matches of the screen text
listed in CONTRACTS.md, SCREEN-1 and SCREEN-2):
- "Yes" to the folder-trust prompt, only for folders in `auto_trust_dirs` (none by default).
- Esc in a window still replying after a second copy of its chat took over the phone, so only
  one copy works on your files.
- "Don't show again" to Claude Code's "Teach auto mode about your environment?" pop-up. This
  changes a Claude Code setting for every session (`autoModeEnvSetup.dismissed` in
  `~/.claude.json`).

**Tools pre-approved for the glasses.** The Even Terminal unit starts Even with
`--claude-allowed-tools` including Bash, Edit and Write, so a prompt from the glasses can run
commands and change files without asking. Edit `systemd/even-terminal.service` to narrow it.

**What listens on the network.** Even Terminal listens on `even_port` (3456). With
`glasses_only_afield` on, Even listens on 127.0.0.1 only and `afield even-proxy` takes the
address Even used (your Tailscale or LAN address). The proxy passes on only token-checked `/api/`
routes, refuses bodies over 1 MB and more than 64 requests at once. Keep both off public networks
(e.g. behind Tailscale); Even's startup banner, which contains a token, is kept out of the logs.

**What it sends out.** Besides the sessions API above (only if you turn it on), only alerts:
with `alerts_url` set, contract failures go there with this machine's hostname. Nothing else
leaves the machine.

**Files.** State (`~/.local/state/afield`, which names your chats and their phone links) is made
private to you (mode 700). Keep `alerts_secret_file` mode 600; afield warns if it is not.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Expect a reply within a week.

Problems in Claude Code or Remote Control themselves go to Anthropic
(https://www.anthropic.com/responsible-disclosure-policy); problems in Even Terminal go to Even Realities.
