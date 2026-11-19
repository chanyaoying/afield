#!/usr/bin/env bash
# Point the live afield paths at this checkout (symlinks) and enable afield.target.
#
#   ./install.sh                 core: RC server, tmux, supervisor (Even Terminal left as it is)
#   ./install.sh --with-even     also the Even Terminal server, for the Even G2 glasses
#   ./install.sh --without-even  remove the Even Terminal service (sessions run without the glasses)
#
# Existing plain files are kept as *.pre-repo. Restarts the supervisor (sessions keep running).
# Changing Even Terminal (installing, removing or migrating it) restarts the glasses wrapper, and
# with it every session that uses it; the supervisor brings them back on their links (~1 min).
# In that case run it from outside an afield session (e.g. `systemd-run --user ./install.sh ...`):
# a session that runs it is one of the sessions that restart.
set -euo pipefail
REPO=$(cd "$(dirname "$0")" && pwd)
UNITS=~/.config/systemd/user
EVEN=keep
case "${1:-}" in
  --with-even) EVEN=install ;;
  --without-even) EVEN=remove ;;
  "") ;;
  *) sed -n '2,12p' "$0" >&2; exit 2 ;;
esac

link() {  # link <repo file> <live path> [derived: the repo copy is meant to differ]
  local src=$REPO/$1 dst=$2 derived=${3:-}
  mkdir -p "$(dirname "$dst")"
  if [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$src" ]; then echo "ok      $dst"; return; fi
  if [ -e "$dst" ] && ! [ -L "$dst" ]; then
    if [ -z "$derived" ] && ! cmp -s "$src" "$dst"; then
      echo "DIFFERS $dst (live copy differs from the repo; commit it first, or rerun with FORCE=1)" >&2
      [ "${FORCE:-}" = 1 ] || exit 1
    fi
    mv "$dst" "$dst.pre-repo"
  fi
  ln -sfn "$src" "$dst"; echo "linked  $dst"
}

# Run through systemd-run (as an upgrade must be), PATH is systemd's bare one, without the folders
# where claude (~/.local/bin) and npm's global packages live. The drop-ins below are written from
# what is found here, so add those folders, and stop before changing anything if a needed binary
# is still missing (2026-11-10: Even crash-looped 298 times on a PATH without even-terminal).
addpath() { if [ -d "$1" ]; then case ":$PATH:" in *":$1:"*) ;; *) PATH="$PATH:$1" ;; esac; fi; }
for d in "$HOME/.local/bin" "$HOME/.npm-global/bin" "$HOME/.bun/bin"; do addpath "$d"; done
addpath "$(npm prefix -g 2>/dev/null)/bin"  # after the loop: npm itself may live in ~/.local/bin
command -v claude >/dev/null || { echo "claude is not on PATH ($PATH); install Claude Code first. Nothing changed." >&2; exit 1; }
if [ $EVEN = install ] || { [ $EVEN = keep ] && [ -L $UNITS/even-terminal.service ]; }; then
  command -v even-terminal >/dev/null || { echo "even-terminal is not on PATH ($PATH); install it or use --without-even. Nothing changed." >&2; exit 1; }
fi

# What afield needs that a fresh machine may lack. Linux with systemd (user services) only.
missing=""
for p in tmux python3 systemctl; do command -v $p >/dev/null || missing="$missing $p"; done
[ -z "$missing" ] || { echo "missing:$missing (install them first). Nothing changed." >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "afield needs Python 3.10 or newer. Nothing changed." >&2; exit 1; }
systemctl --user show-environment >/dev/null 2>&1 || { echo "no systemd user manager (systemctl --user); afield needs one. Nothing changed." >&2; exit 1; }
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
  echo "NOTE: lingering is off, so sessions stop when you log out and do not start at boot." >&2
  echo "      To keep them running: sudo loginctl enable-linger $USER" >&2
fi
grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null || \
  echo "NOTE: the cgroup v2 memory controller is not available; memory_max_gb will not be enforced." >&2

for t in test_afield test_contracts; do
  python3 -I "$REPO/tests/$t.py" >/dev/null 2>&1 || { echo "tests/$t.py fails; nothing changed" >&2; exit 1; }
done

# afield was called claude-rc. An install under that name is moved over once: its units are stopped
# (this ends every session; the new supervisor brings them back on their phone links, ~1 min), its
# links, wants and drop-ins removed, and its config and state moved. Sessions keep running in the
# old served folder (~/claude-rc), because their transcripts are filed under it.
OLDU="claude-rc-supervisor.service claude-rc.service claude-rc-tmux.service"
if [ -e $UNITS/claude-rc.target ] || [ -L $UNITS/claude-rc.target ] || [ -L ~/.local/bin/claude-rc ]; then
  echo "migrating from claude-rc"
  systemctl --user stop $OLDU claude-rc.target 2>/dev/null || true
  if [ -L $UNITS/even-terminal.service ] && [ $EVEN = keep ]; then EVEN=install; fi
  systemctl --user stop even-terminal.service 2>/dev/null || true
  rm -rf $UNITS/claude-rc.target.wants $UNITS/claude-rc*.d
  rm -f $UNITS/default.target.wants/claude-rc.target $UNITS/claude-rc*.service $UNITS/claude-rc.slice \
    $UNITS/claude-rc.target $UNITS/even-terminal.service.d/50-claude-rc-config.conf \
    ~/.local/bin/claude-rc ~/.claude/skills/claude-rc
  rm -rf ~/.local/share/claude-rc
  OLDCONF=${XDG_CONFIG_HOME:-$HOME/.config}/claude-rc NEWCONF=${XDG_CONFIG_HOME:-$HOME/.config}/afield
  if [ -d "$OLDCONF" ] && ! [ -e "$NEWCONF" ]; then mv "$OLDCONF" "$NEWCONF"; echo "moved   $OLDCONF -> $NEWCONF"; fi
  NEWSTATE=${XDG_STATE_HOME:-$HOME/.local/state}/afield
  if [ -d ~/.claude/rc-sessions ] && ! [ -e "$NEWSTATE" ]; then
    mkdir -p "$(dirname "$NEWSTATE")"; mv ~/.claude/rc-sessions "$NEWSTATE"; echo "moved   ~/.claude/rc-sessions -> $NEWSTATE"
  fi
  if [ -d ~/claude-rc ]; then  # keep serving the old folder unless the config already names one
    mkdir -p "$NEWCONF"
    python3 -I - "$NEWCONF/config.json" <<'PY'
import json, os, sys
path = sys.argv[1]
conf = json.load(open(path)) if os.path.exists(path) else {}
if "served_dir" not in conf:
    conf["served_dir"] = "~/claude-rc"
    with open(path + ".tmp", "w") as f:
        json.dump(conf, f, indent=2); f.write("\n")
    os.replace(path + ".tmp", path)
    print(f"set     served_dir ~/claude-rc in {path}")
PY
  fi
  systemctl --user daemon-reload
fi

link bin/afield                   ~/.local/bin/afield
link tests/test_afield.py         ~/.local/share/afield/test_afield.py derived  # repo copy finds the script itself
link claude/commands/afield-retire.md ~/.claude/commands/afield-retire.md
# /retire was renamed /afield-retire, so it cannot clash with another tool's /retire.
if [ -L ~/.claude/commands/retire.md ] && [ "$(readlink ~/.claude/commands/retire.md)" = "$REPO/claude/commands/retire.md" ]; then
  rm ~/.claude/commands/retire.md; echo "removed ~/.claude/commands/retire.md (now /afield-retire)"
fi
link claude/skills/afield         ~/.claude/skills/afield
CORE="afield.service afield-tmux.service afield-supervisor.service"
for u in afield.slice afield.target $CORE; do
  link systemd/$u $UNITS/$u
done

# An Even Terminal from before afield.target ran in a tmux session called "even" on the default
# socket, outside its unit's control; stop that one by hand so the new unit can take the port.
old_even() { env -u TMUX tmux -L default has-session -t even 2>/dev/null; }

case $EVEN in
  install)
    link systemd/even-terminal.service $UNITS/even-terminal.service ;;
  remove)
    systemctl --user disable --now even-terminal.service 2>/dev/null || true
    if old_even; then env -u TMUX tmux -L default kill-session -t even; fi
    rm -f $UNITS/even-terminal.service.d/50-afield-config.conf
    if [ -L $UNITS/even-terminal.service ]; then rm $UNITS/even-terminal.service; echo "removed $UNITS/even-terminal.service"; fi ;;
esac
# The glasses proxy (afield even-proxy) only makes sense with Even Terminal and glasses_only_afield on.
PROXY=off
if [ -e $UNITS/even-terminal.service ] || [ $EVEN = install ]; then
  if [ "$(python3 -I "$REPO/bin/afield" config glasses_only_afield)" = true ]; then
    if python3 -I "$REPO/bin/afield" even-proxy --address >/dev/null; then PROXY=on
    else echo "WARNING: the glasses will list every session (see above)" >&2; fi
  fi
fi
case $PROXY in
  on) link systemd/afield-even-proxy.service $UNITS/afield-even-proxy.service ;;
  off)
    systemctl --user stop afield-even-proxy.service 2>/dev/null || true
    rm -f $UNITS/afield.target.wants/afield-even-proxy.service
    if [ -L $UNITS/afield-even-proxy.service ]; then rm $UNITS/afield-even-proxy.service; echo "removed $UNITS/afield-even-proxy.service"; fi ;;
esac

# Settings from ~/.config/afield/config.json (afield config) that systemd needs, as drop-ins next
# to the linked units: the served folder, the memory cap, Even's port, and a PATH that finds claude
# and even-terminal wherever they are installed. Regenerated on every run.
setting() { python3 -I "$REPO/bin/afield" config "$1"; }
SERVED=$(setting served_dir)
MAXGB=$(setting memory_max_gb)
mkdir -p "$SERVED"
paths=""
for p in claude even-terminal tmux python3; do
  if d=$(dirname "$(command -v $p)" 2>/dev/null) && [ "$d" != . ]; then
    case ":$paths:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:" in *":$d:"*) ;; *) paths="$paths${paths:+:}$d" ;; esac
  fi
done
command -v claude >/dev/null || echo "WARNING: claude is not on PATH; install Claude Code first" >&2
dropin() {  # dropin <unit> <lines...>
  local dir=$UNITS/$1.d; shift
  mkdir -p "$dir"
  printf '%s\n' "# written by afield install.sh from $(python3 -I "$REPO/bin/afield" config | head -1 | cut -c3-)" "$@" \
    > "$dir/50-afield-config.conf"
}
ENVPATH="Environment=PATH=${paths:+$paths:}$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
dropin afield.service "[Service]" "WorkingDirectory=$SERVED" "$ENVPATH"
dropin afield-tmux.service "[Service]" "WorkingDirectory=$SERVED" "$ENVPATH"
dropin afield-supervisor.service "[Service]" "$ENVPATH"
dropin afield.slice "[Slice]" "MemoryMax=${MAXGB}G" "MemoryHigh=$(python3 -c "print(round($MAXGB * 0.75, 2))")G"
EVEN_CHANGED=no
if [ -e $UNITS/even-terminal.service ]; then
  before=$(cat $UNITS/even-terminal.service.d/50-afield-config.conf 2>/dev/null || true)
  if [ $PROXY = on ]; then  # Even on 127.0.0.1 only; the proxy takes the glasses' address
    dropin even-terminal.service "[Service]" "$ENVPATH" "Environment=EVEN_PORT=$(setting even_port)" \
      "Environment=EVEN_HOST_MODE=interface EVEN_HOST_INTERFACE=lo"
  else
    dropin even-terminal.service "[Service]" "$ENVPATH" "Environment=EVEN_PORT=$(setting even_port)"
  fi
  [ "$before" = "$(cat $UNITS/even-terminal.service.d/50-afield-config.conf)" ] || EVEN_CHANGED=yes
fi
echo "settings $(python3 -I "$REPO/bin/afield" config | head -1 | cut -c3-) -> drop-ins (served folder $SERVED)"

# Never `reenable`/`disable` here: these units are links into the repo, and disabling a linked unit
# deletes the link itself. `enable` is idempotent; links from before afield.target (WantedBy=
# default.target) are removed by hand.
unwant() { local w=$UNITS/default.target.wants/$1; if [ -L "$w" ]; then rm "$w"; echo "unwanted default.target -> $1"; fi; }
systemctl --user daemon-reload
for u in $CORE; do unwant $u; done
systemctl --user enable -q $CORE afield.target
echo "enabled afield.target ($CORE)"

if [ $EVEN = install ] || { [ $EVEN = keep ] && [ -e $UNITS/even-terminal.service ]; }; then
  unwant even-terminal.service
  systemctl --user enable -q even-terminal.service
  if old_even; then
    echo "moving Even Terminal out of its old tmux session (wrapped sessions restart, ~1 min)"
    systemctl --user stop even-terminal.service 2>/dev/null || true
    env -u TMUX tmux -L default kill-session -t even 2>/dev/null || true
  fi
  if [ $EVEN_CHANGED = yes ] && systemctl --user -q is-active even-terminal.service; then
    echo "restarting Even Terminal for its new settings (wrapped sessions restart, ~1 min)"
    systemctl --user restart even-terminal.service
  fi
  systemctl --user start even-terminal.service
  echo "Even Terminal: $(systemctl --user is-active even-terminal.service)"
fi
if [ $PROXY = on ]; then
  systemctl --user enable -q afield-even-proxy.service
  systemctl --user restart afield-even-proxy.service
  echo "glasses proxy: $(systemctl --user is-active afield-even-proxy.service) on $(python3 -I "$REPO/bin/afield" even-proxy --address):$(setting even_port)"
fi

systemctl --user start afield.target
systemctl --user restart afield-supervisor.service
echo "done; supervisor restarted"
# The contracts: is everything afield relies on in Claude Code / Even still the same? (CONTRACTS.md)
python3 -I "$REPO/bin/afield" doctor | tail -2 || echo "a contract failed: see afield doctor and CONTRACTS.md" >&2
