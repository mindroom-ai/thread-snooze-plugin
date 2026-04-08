# Thread Snooze Plugin

MindRoom plugin for snoozing threads — temporarily resolve a thread and automatically unresolve it at a specified time.

## How It Works

1. Tag a thread with `snoozed` (set `data.until` to an ISO-8601 datetime)
2. The thread is resolved (hidden from active view)
3. At the specified time, the thread auto-unresolves and a notification is posted

## Tools

- `snooze_thread(until, note=None)` — snooze the current thread until the given ISO-8601 datetime (UTC). Adds `snoozed` + `resolved` tags and schedules a wake task.
- `unsnooze_thread()` — cancel an active snooze. Removes `snoozed` + `resolved` tags and cancels the wake task.

## Hooks

- `resume_snoozed_threads` on `bot:ready` (priority 90) — rescans all rooms on restart, re-creates wake tasks for any threads still tagged `snoozed`. Expired snoozes fire immediately.
- `schedule_manual_snooze_tag` on `tool:after_call` (priority 100) — detects when someone uses the generic `tag_thread("snoozed")` or `untag_thread("snoozed")` tools and spawns/cancels wake tasks accordingly.

## Tag Format

- Tag name: `snoozed`
- Tag data: `{"until": "2026-04-10T09:00:00+00:00", "note": "optional note"}`
- All datetimes in UTC. Agent is responsible for timezone conversion.

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

- **Zero core changes** — pure plugin
- **Tags are the persistence layer** — no separate state events or database
- **`asyncio.create_task()`** for wake timing — tasks die on restart, `bot:ready` rescans and re-creates them
- **Race-safe** — wake tasks verify tag state before clearing (prevents stale wake from erasing a newer snooze)

## License

MIT