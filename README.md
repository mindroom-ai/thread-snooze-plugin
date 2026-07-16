# Thread Snooze

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Snooze threads for [MindRoom](https://github.com/mindroom-ai/mindroom) agents: temporarily resolve a thread now and wake it automatically later.

When a thread needs attention later but not now, snooze it. The thread is resolved out of the active view, and when the target time arrives it is un-resolved and a reminder message is posted so the work comes back into view. Persistence is tag-based: no database, no external scheduler, just Matrix state plus in-process wake tasks.

## Features

- Dedicated `snooze_thread` and `unsnooze_thread` tools for the current thread
- Automatically adds both `snoozed` and `resolved` tags when a snooze is created
- Restores wake timers on `bot:ready` after a restart
- Interops with generic `tag_thread` and `untag_thread` when the `snoozed` tag is used
- Accepts ISO-8601 timestamps and treats naive datetimes as UTC
- Uses race-safe wake logic so stale timers do not clear a newer snooze

## How It Works

1. An agent calls `snooze_thread(until=...)`, or another tool writes the `snoozed` tag with a valid `data.until` timestamp.
2. The plugin marks the thread as resolved and starts an in-process wake task.
3. When the target time arrives, the plugin removes both `snoozed` and `resolved`, then posts `⏰ Snooze expired`.
4. If MindRoom restarts before the wake time, the `thread-snooze-resume` hook rescans room state and rebuilds the missing wake tasks.
5. If a snooze is already expired during startup, it fires immediately instead of waiting again.

## Agent Tools

| Tool | Purpose |
|------|---------|
| `snooze_thread(until, note=None)` | Snooze the current thread until an ISO-8601 datetime. Adds `snoozed` and `resolved` tags |
| `unsnooze_thread()` | Cancel an active snooze, remove the tags, and cancel the wake task |

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `thread-snooze-resume` | `bot:ready` | Rescan all rooms on startup and recreate wake tasks for active snoozes |
| `thread-snooze-detect-tag` | `tool:after_call` | Detect manual `tag_thread("snoozed")` and `untag_thread("snoozed")` operations and mirror them into wake-task state |

## Tag Format

- Tag name: `snoozed`
- Tag data: `{"until": "2026-04-10T09:00:00+00:00", "note": "optional"}`
- Stored wake times are normalized to UTC
- The `resolved` tag is also added while the thread is snoozed

## Install

Vendor this plugin with the MindRoom CLI:

```bash
mindroom plugins install thread-snooze-plugin
```

Then reference it from `config.yaml`:

```yaml
plugins:
  - path: plugins/thread-snooze-plugin
```

Update to the latest commit later with:

```bash
mindroom plugins update thread-snooze-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
It requires a MindRoom release newer than v2026.7.175.
For a manual checkout instead, see Setup below.

## Setup

1. Copy this plugin to `~/.mindroom/plugins/thread-snooze`.
2. Add the plugin to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/thread-snooze
   ```
3. Add `thread_snooze` to the agent's tools list.
4. Restart MindRoom.

## Notes

- Wake timers are rebuilt after restart from the current snooze tag state.
- The wake path verifies current tag state before clearing anything, which prevents an old timer from erasing a newer snooze.
