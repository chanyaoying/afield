#!/usr/bin/env bash
# Remove afield: stop its services and delete what install.sh put in place.
#
#   ./uninstall.sh --yes
#
# Stopping afield.target stops every session it hosts (their transcripts stay; `claude --resume
# <id>` reopens one by hand). Kept: your config (~/.config/afield), afield's state
# (~/.local/state/afield) and every transcript. Delete those two folders yourself for a clean slate.
# Run it from outside an afield session (e.g. `systemd-run --user ./uninstall.sh --yes`).
set -euo pipefail
[ "${1:-}" = --yes ] || { sed -n '2,9p' "$0" >&2; exit 2; }
UNITS=~/.config/systemd/user
ALL="afield.target afield-supervisor.service afield.service afield-tmux.service even-terminal.service afield-even-proxy.service"

systemctl --user stop afield.target $ALL 2>/dev/null || true
# Here disabling is what we want: it removes the links, including the unit files themselves.
systemctl --user disable $ALL 2>/dev/null || true
for u in $ALL afield.slice; do
  rm -f "$UNITS/$u.d/50-afield-config.conf"
  rmdir "$UNITS/$u.d" 2>/dev/null || true
  if [ -L "$UNITS/$u" ]; then rm "$UNITS/$u"; echo "removed $UNITS/$u"; fi
done
systemctl --user daemon-reload
for f in ~/.local/bin/afield ~/.local/share/afield/test_afield.py ~/.claude/commands/afield-retire.md ~/.claude/commands/retire.md ~/.claude/skills/afield; do
  if [ -L "$f" ]; then rm "$f"; echo "removed $f"; fi
done
echo "afield removed. Kept: ~/.config/afield, ~/.local/state/afield and your transcripts."
