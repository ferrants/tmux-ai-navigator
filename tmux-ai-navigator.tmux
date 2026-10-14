#!/usr/bin/env bash
# tmux plugin entry (TPM-compatible): key bindings, the hooks that track what you've seen,
# and the notification watcher. Options (set them before TPM / this file runs):
#   set -g @ainav-key 'a'          # prefix key that opens/closes the picker (default: a)
#   set -g @ainav-later-key 'u'    # prefix key that toggles "later" on the current pane's agent
#   set -g @ainav-popup 'on'       # tmux 3.2+: open in a popup (on) or a temporary window (off)
#   set -g @ainav-popup-width '90%'
#   set -g @ainav-popup-height '85%'
#   set -g @ainav-extras 'cmd'     # extras command (badge/title/preview lines per session; see README)
#   set -g @ainav-extras-interval 5
#   set -g @ainav-notify 'tmux,system'  # notifications: tmux, system, terminal, bell, or off
# Manual install: run-shell /path/to/tmux-ai-navigator/tmux-ai-navigator.tmux
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/bin/tmux-ai-nav"
opt() { local v; v="$(tmux show -gqv "$1")"; echo "${v:-$2}"; }

# tmux -V: "tmux 3.0a", "tmux 3.5", "tmux next-3.6", "tmux master", "tmux openbsd-7.6"
tmux_at_least() {
  local v
  v="$(tmux -V 2>/dev/null | sed -E 's/^tmux (next-)?//')"
  case "$v" in
    master|openbsd-*) return 0 ;;
  esac
  v="$(printf '%s' "$v" | sed -E 's/^([0-9]+)\.([0-9]+).*/\1 \2/')"
  set -- $v "$@"
  [ "$1" -gt "$3" ] 2>/dev/null || { [ "$1" -eq "$3" ] 2>/dev/null && [ "$2" -ge "$4" ] 2>/dev/null; }
}

# Bind a prefix key, but don't take over one the user (or another plugin) already uses,
# unless they chose it explicitly with the option.
bind_prefix() {
  local option="$1" default="$2"; shift 2
  local chosen key existing
  chosen="$(tmux show -gqv "$option")"
  key="${chosen:-$default}"
  # the key follows "-T prefix", after optional flags like -r
  existing="$(tmux list-keys -T prefix 2>/dev/null |
    awk -v k="$key" '{ for (i = 2; i < NF; i++) if ($i == "prefix") { if ($(i + 1) == k) print; break } }' | head -1)"
  if [ -z "$chosen" ] && [ -n "$existing" ] && ! printf '%s' "$existing" | grep -q 'tmux-ai-nav'; then
    tmux display-message "tmux-ai-navigator: prefix $key is already bound; set $option to pick a key"
    return
  fi
  tmux bind-key "$key" "$@"
}

if tmux_at_least 3 2 && [ "$(opt @ainav-popup on)" != "off" ]; then
  size="$(opt @ainav-popup-width 90%)x$(opt @ainav-popup-height 85%)"
  bind_prefix @ainav-key a run-shell -b "'$BIN' -S '#{socket_path}' open --client '#{client_tty}' --pane '#{pane_id}' --popup '$size'"
else
  bind_prefix @ainav-key a run-shell -b "'$BIN' open --client '#{client_tty}' --pane '#{pane_id}'"
fi
bind_prefix @ainav-later-key u run-shell -b "'$BIN' later --pane '#{pane_id}'"

# "done" = finished since you last looked: record which panes are on screen whenever that
# changes. Fixed array index so re-sourcing the config replaces instead of duplicating.
for hook in after-select-pane after-select-window session-window-changed client-session-changed client-attached; do
  tmux set-hook -g "$hook[87]" "run-shell -b \"'$BIN' seen\""
done
# leaving the screen by detaching counts too; the detaching client is still listed while
# its hook runs, so look a moment later (the detach time is what gets recorded)
tmux set-hook -g "client-detached[87]" "run-shell -b \"'$BIN' seen --delay 0.5\""

# notifications when an agent finishes or needs you (see README; @ainav-notify 'off' disables)
tmux run-shell -b "'$BIN' watch --start"
