# Thread Snooze

[![License](https://img.shields.io/github/license/mindroom-ai/thread-snooze-plugin)](https://github.com/mindroom-ai/thread-snooze-plugin/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Snooze threads for [MindRoom](https://github.com/mindroom-ai/mindroom) agents — temporarily resolve a thread and automatically unresolve it at a specified time.

When a thread needs attention later but not now, snooze it. The thread gets resolved (hidden from active view), and at the specified time it unresolves and posts a notification so nothing falls through the cracks. Tags are the persistence layer — no database, no scheduler integration, just Matrix state events and `asyncio` tasks.

## How it works

1. Agent calls `snooze_thread(until="2026-04-10T09:00")` or user tags a thread with `snoozed` (with `data.until` set to an ISO-8601 datetime)
2. Thread is resolved — disappears from active view
3. Plugin spawns an `asyncio` task that sleeps until the target time
4. When the snooze expires: `snoozed` and `resolved` tags are removed, a notification is posted, and the thread reappears
5. On restart, `bot:ready` rescans all rooms and re-creates wake tasks for any still-snoozed threads. Expired snoozes fire immediately.

## Agent tools

| Tool | Purpose |
|------|---------|
| `snooze_thread(until, note=None)` | Snooze the current thread until an ISO-8601 datetime (UTC). Adds `snoozed` + `resolved` tags. |
| `unsnooze_thread()` | Cancel an active snooze. Removes tags and cancels the wake task. |

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `resume_snoozed_threads` | `bot:ready` | Rescan all rooms on startup, re-create wake tasks (priority 90) |
| `schedule_manual_snooze_tag` | `tool:after_call` | Detect manual `tag_thread("snoozed")` / `untag_thread("snoozed")` and spawn/cancel wake tasks (priority 100) |

## Tag format

- **Tag name:** `snoozed`
- **Tag data:** `{"until": "2026-04-10T09:00:00+00:00", "note": "optional"}`
- All datetimes in UTC. The agent is responsible for timezone conversion.

## Setup

1. Copy to `~/.mindroom/plugins/thread-snooze`
2. Add to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/thread-snooze
   ```
3. Add `thread_snooze` to agent's tools list
4. Restart MindRoom

## Architecture

- **Pure plugin** — no MindRoom core changes required
- **Tags are the persistence layer** — the `snoozed` tag with `data.until` is the single source of truth
- **`asyncio.create_task()`** for wake timing — tasks die on restart, `bot:ready` rescans and re-creates them
- **Race-safe** — wake tasks verify tag state before clearing (prevents stale wake from erasing a newer snooze)
- **Interop** — works with both the dedicated tools and the generic `tag_thread` / `untag_thread` tools