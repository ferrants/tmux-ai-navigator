#!/usr/bin/env bash
# tmux plugin entry (TPM-compatible): key bindings and the hooks that track what you've seen.
#   set -g @ainav-key 'a'          # prefix key that opens/closes the picker (default: a)
#   set -g @ainav-later-key 'u'    # prefix key that toggles "later" on the current pane's agent
#   set -g @ainav-extras 'cmd'     # extras command (badge/title/preview lines per session; see README)
#   set -g @ainav-extras-interval 5
#   set -g @ainav-notify 'tmux,system'  # notifications: tmux, system, terminal, bell, or off
# Manual install: run-shell /path/to/tmux-ai-navigator/tmux-ai-navigator.tmux
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/bin/tmux-ai-nav"
opt() { local v; v="$(tmux show -gqv "$1")"; echo "${v:-$2}"; }

tmux bind-key "$(opt @ainav-key a)" run-shell -b "'$BIN' open --client '#{client_tty}' --pane '#{pane_id}'"
tmux bind-key "$(opt @ainav-later-key u)" run-shell -b "'$BIN' later --pane '#{pane_id}'"

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
