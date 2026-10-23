# Changelog

## Unreleased

- Tabs: `tab` now switches between all, the tmux session and the project (git repo) you
  opened the picker from, each with its own search and cursor. The status filters it used to
  cycle are gone; the sections already put waiting and working sessions first.
- "↺ back": after a jump, the picker reopens (within `@ainav-resume` minutes, default 10) on
  the view you left, with the cursor on the next row, if you'd searched, changed tab,
  expanded "older" or grouped. `@ainav-default-tab` picks the tab to open on otherwise.
- Search also matches the preview's last prompt and reply, highlighted in the preview.
- The preview renders markdown: tables, lists, headings, bold, inline code, code blocks,
  quotes and links.
- `prefix A` (`@ainav-last-key`): back to the agent pane you were on before, across sessions
  and windows; press again to bounce between the two. Marked `↩` in the picker.
- Codex CLI sessions: every Codex TUI in a tmux pane is listed with Claude's, with status
  (working, waiting for an approval, done), title, timing, preview, later flags and
  notifications. No setup: status is read from the pane title Codex keeps, the conversation
  from Codex's thread database and log.
- Notification sounds: `@ainav-notify-sound 'soft'` (now what `on` means) plays a short,
  quiet built-in chime instead of the desktop's sound theme; `system` keeps the old sounds.
  New `@ainav-notify-volume` (0-100).
- `tmux-ai-nav watch --test done|waiting` sends a sample notification with your settings.

## 0.1.0

First release.

- Picker of Claude Code sessions across all tmux sessions and windows, grouped into needs
  you (waiting, done since you last looked, flagged for later), working, today and older,
  with a preview of the last prompt and reply, search, filters, grouping by project, and a
  jump to the pane.
- Popup on tmux 3.2+, temporary window on tmux 3.0 and 3.1.
- Notifications when a session finishes or needs you: tmux message, desktop, terminal
  (OSC 777), bell, sounds, or a custom command.
- Extras command hook for your own badges, titles, preview lines and search text.
- Linux and macOS.
- `tmux-ai-nav demo` with made-up sessions.
