# Changelog

## Unreleased

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
