# ruff: noqa: INP001
"""Hooks for the MindRoom thread-snooze plugin."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import nio

from mindroom.hooks import AgentLifecycleContext, ToolAfterCallContext, hook
from mindroom.thread_tags import (
    ThreadTagsError,
    ThreadTagsState,
    get_thread_tags,
    list_tagged_threads,
    remove_thread_tag,
)

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from mindroom.thread_tag_store import ThreadTagStore

SNOOZE_TAG = "snoozed"
SNOOZE_EXPIRED_MESSAGE = "\u23f0 Snooze expired"
SNOOZE_WAKE_RETRY_DELAY = timedelta(seconds=30)
_snooze_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

ThreadMessageSender = Callable[[str, str, str | None], Awaitable[str | None]]
WakeCallback = Callable[[], Awaitable[None]]


def parse_snooze_until(value: object) -> datetime | None:
    """Parse one snooze-until value as a UTC datetime."""
    if not isinstance(value, str):
        return None

    normalized_value = value.strip()
    if not normalized_value:
        return None
    if len(normalized_value) < 11 or normalized_value[10] not in {" ", "T", "t"}:
        return None

    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_until_from_tag_content(content: object) -> datetime | None:
    """Extract one snooze timestamp from a thread-tag payload."""
    if not isinstance(content, Mapping):
        return None

    data = content.get("data")
    if not isinstance(data, Mapping):
        return None

    return parse_snooze_until(data.get("until"))


def _snooze_until_from_state(state: ThreadTagsState | None) -> datetime | None:
    """Extract one snooze timestamp from one parsed thread-tag state."""
    if state is None:
        return None
    snoozed = state.tags.get(SNOOZE_TAG)
    if snoozed is None:
        return None
    return parse_snooze_until(snoozed.data.get("until"))


def _parse_tool_result_payload(result: object) -> dict[str, Any] | None:
    """Parse one JSON tool result payload."""
    if not isinstance(result, str):
        return None

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None
    return parsed


def _parse_until_from_tool_payload(payload: Mapping[str, object]) -> datetime | None:
    """Extract one snooze timestamp from a tag_thread result payload."""
    raw_tags = payload.get("tags")
    if not isinstance(raw_tags, Mapping):
        return None

    snoozed_tag = raw_tags.get(SNOOZE_TAG)
    if not isinstance(snoozed_tag, Mapping):
        return None

    return _parse_until_from_tag_content(snoozed_tag)


def _cancel_snooze_task(room_id: str, thread_root_id: str) -> None:
    """Cancel and forget one running snooze task."""
    task = _snooze_tasks.pop((room_id, thread_root_id), None)
    if task is None or task.done():
        return
    task.cancel()


def _spawn_snooze_task(
    room_id: str,
    thread_root_id: str,
    until: datetime,
    *,
    wake: WakeCallback,
    logger: BoundLogger,
) -> asyncio.Task[None]:
    """Start or replace the in-process wake task for one snoozed thread."""
    key = (room_id, thread_root_id)
    _cancel_snooze_task(room_id, thread_root_id)

    task: asyncio.Task[None]

    async def _runner() -> None:
        should_forget_task = False
        try:
            delay_seconds = (until - datetime.now(UTC)).total_seconds()
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await wake()
            should_forget_task = True
        except asyncio.CancelledError:
            logger.info("Cancelled snooze task", room_id=room_id, thread_id=thread_root_id)
            should_forget_task = True
            raise
        except Exception:
            logger.exception("Snooze task failed", room_id=room_id, thread_id=thread_root_id)
            raise
        finally:
            if should_forget_task and _snooze_tasks.get(key) is task:
                _snooze_tasks.pop(key, None)

    task = asyncio.create_task(_runner(), name=f"thread-snooze:{room_id}:{thread_root_id}")
    _snooze_tasks[key] = task
    logger.info("Scheduled snooze task", room_id=room_id, thread_id=thread_root_id, until=until.isoformat())
    return task


def _retry_snooze_wake(
    *,
    room_id: str,
    thread_root_id: str,
    expected_until: datetime,
    tag_cleared: bool,
    client: nio.AsyncClient,
    store: ThreadTagStore,
    send_message: ThreadMessageSender,
    logger: BoundLogger,
) -> None:
    """Re-arm one wake task after a transient Matrix failure."""
    retry_at = datetime.now(UTC) + SNOOZE_WAKE_RETRY_DELAY
    key = (room_id, thread_root_id)
    if _snooze_tasks.get(key) is asyncio.current_task():
        _snooze_tasks.pop(key, None)

    async def wake() -> None:
        await _wake_thread(
            room_id=room_id,
            thread_root_id=thread_root_id,
            expected_until=expected_until,
            tag_cleared=tag_cleared,
            client=client,
            store=store,
            send_message=send_message,
            logger=logger,
        )

    _spawn_snooze_task(
        room_id,
        thread_root_id,
        retry_at,
        wake=wake,
        logger=logger,
    )


def _should_skip_wake_for_stale_snooze(
    *,
    room_id: str,
    thread_root_id: str,
    expected_until: datetime,
    current_until: datetime | None,
    tag_cleared: bool,
    logger: BoundLogger,
) -> bool:
    """Return whether the wake should be skipped because state no longer matches."""
    if not tag_cleared and current_until != expected_until:
        logger.info(
            "Skipping stale snooze wake",
            room_id=room_id,
            thread_id=thread_root_id,
            expected_until=expected_until.isoformat(),
            current_until=None if current_until is None else current_until.isoformat(),
        )
        return True

    if tag_cleared and current_until is not None:
        logger.info(
            "Skipping stale snooze wake after prior tag clear",
            room_id=room_id,
            thread_id=thread_root_id,
            expected_until=expected_until.isoformat(),
            current_until=current_until.isoformat(),
        )
        return True

    return False


async def _send_snooze_expired_notice(
    *,
    room_id: str,
    thread_root_id: str,
    expected_until: datetime,
    tag_cleared: bool,
    client: nio.AsyncClient,
    store: ThreadTagStore,
    send_message: ThreadMessageSender,
    logger: BoundLogger,
) -> None:
    """Send the wake notice or reschedule the wake when the send fails."""
    event_id = await send_message(room_id, SNOOZE_EXPIRED_MESSAGE, thread_root_id)
    if event_id is not None:
        return

    logger.warning(
        "Failed to send snooze expiry message; scheduling retry",
        room_id=room_id,
        thread_id=thread_root_id,
    )
    _retry_snooze_wake(
        room_id=room_id,
        thread_root_id=thread_root_id,
        expected_until=expected_until,
        tag_cleared=tag_cleared,
        client=client,
        store=store,
        send_message=send_message,
        logger=logger,
    )


async def _wake_thread(
    *,
    room_id: str,
    thread_root_id: str,
    expected_until: datetime,
    tag_cleared: bool = False,
    client: nio.AsyncClient,
    store: ThreadTagStore,
    send_message: ThreadMessageSender,
    logger: BoundLogger,
) -> None:
    """Wake one snoozed thread by clearing its tag and posting one notice."""
    try:
        current_until = _snooze_until_from_state(await get_thread_tags(store, room_id, thread_root_id))
        # Give the sync loop one turn to project a just-arrived resnooze before we clear or notify.
        await asyncio.sleep(0)
        refreshed_until = _snooze_until_from_state(await get_thread_tags(store, room_id, thread_root_id))
        if refreshed_until != current_until:
            current_until = refreshed_until
        normalized_expected_until = expected_until.astimezone(UTC)
        if _should_skip_wake_for_stale_snooze(
            room_id=room_id,
            thread_root_id=thread_root_id,
            expected_until=normalized_expected_until,
            current_until=current_until,
            tag_cleared=tag_cleared,
            logger=logger,
        ):
            return

        if not tag_cleared:
            verified_state = await remove_thread_tag(
                client,
                store,
                room_id,
                thread_root_id,
                SNOOZE_TAG,
            )
            if verified_state.tags.get(SNOOZE_TAG) is not None:
                updated_until = _snooze_until_from_state(verified_state)
                logger.info(
                    "Skipping stale snooze wake after concurrent update",
                    room_id=room_id,
                    thread_id=thread_root_id,
                    expected_until=normalized_expected_until.isoformat(),
                    current_until=None if updated_until is None else updated_until.isoformat(),
                )
                return
            tag_cleared = True

        await _send_snooze_expired_notice(
            room_id=room_id,
            thread_root_id=thread_root_id,
            expected_until=expected_until,
            tag_cleared=tag_cleared,
            client=client,
            store=store,
            send_message=send_message,
            logger=logger,
        )
    except ThreadTagsError:
        logger.warning(
            "Failed to clear thread tag during snooze wake; scheduling retry",
            room_id=room_id,
            thread_id=thread_root_id,
            tag=SNOOZE_TAG,
            exc_info=True,
        )
        _retry_snooze_wake(
            room_id=room_id,
            thread_root_id=thread_root_id,
            expected_until=expected_until,
            tag_cleared=tag_cleared,
            client=client,
            store=store,
            send_message=send_message,
            logger=logger,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Wake thread hit transport failure; scheduling retry",
            room_id=room_id,
            thread_id=thread_root_id,
            exc_info=True,
        )
        _retry_snooze_wake(
            room_id=room_id,
            thread_root_id=thread_root_id,
            expected_until=expected_until,
            tag_cleared=tag_cleared,
            client=client,
            store=store,
            send_message=send_message,
            logger=logger,
        )


def _manual_snooze_tag_target(
    payload: Mapping[str, object],
    ctx: ToolAfterCallContext,
) -> tuple[str, str] | None:
    """Return the observed manual snooze target room and thread."""
    room_id = payload.get("room_id") if isinstance(payload.get("room_id"), str) else ctx.room_id
    thread_root_id = payload.get("thread_id") if isinstance(payload.get("thread_id"), str) else ctx.thread_id
    if room_id is None or thread_root_id is None:
        return None
    return room_id, thread_root_id


def _manual_snooze_tag_until(
    payload: Mapping[str, object],
    arguments: Mapping[str, object],
) -> datetime | None:
    """Return the observed manual snooze wake timestamp."""
    until = _parse_until_from_tool_payload(payload)
    if until is not None:
        return until

    data = arguments.get("data")
    if not isinstance(data, Mapping):
        return None
    return parse_snooze_until(data.get("until"))


def _manual_snooze_change(ctx: ToolAfterCallContext) -> tuple[dict[str, Any], str, str] | None:
    """Return one successful manual snooze-tool change with its target scope."""
    if ctx.blocked or ctx.error is not None:
        return None
    if ctx.tool_name not in {"tag_thread", "untag_thread"}:
        return None

    raw_tag = ctx.arguments.get("tag")
    if not isinstance(raw_tag, str) or raw_tag.strip().lower() != SNOOZE_TAG:
        return None

    payload = _parse_tool_result_payload(ctx.result)
    if payload is None or payload.get("status") != "ok":
        return None

    target = _manual_snooze_tag_target(payload, ctx)
    if target is None:
        return None
    room_id, thread_root_id = target
    return payload, room_id, thread_root_id


@hook(
    event="bot:ready",
    name="thread-snooze-resume",
    priority=90,
    timeout_ms=120000,
)
async def resume_snoozed_threads(ctx: AgentLifecycleContext) -> None:  # noqa: C901
    """Recreate in-process wake timers for all currently snoozed threads."""
    ctx.logger.info(
        "Resuming snoozed threads after bot ready",
        entity_name=ctx.entity_name,
        room_count=len(ctx.joined_room_ids),
    )
    if ctx.thread_tag_store is None:
        ctx.logger.warning("No thread tag store available for snoozed-thread resume")
        return
    if ctx.matrix_client is None:
        ctx.logger.warning("No Matrix client available for snoozed-thread resume")
        return
    if ctx.message_sender is None:
        ctx.logger.warning("No message sender available for snoozed-thread resume")
        return

    async def send_message(room_id: str, text: str, thread_id: str | None) -> str | None:
        return await ctx.send_message(room_id, text, thread_id=thread_id)

    for room_id in ctx.joined_room_ids:
        try:
            snoozed_threads = await list_tagged_threads(ctx.thread_tag_store, room_id, tag=SNOOZE_TAG)
        except asyncio.CancelledError:
            raise
        except Exception:
            ctx.logger.warning(
                "Failed to scan thread tag store for snoozed threads",
                room_id=room_id,
                exc_info=True,
            )
            continue
        for thread_root_id, thread_state in snoozed_threads.items():
            until = _snooze_until_from_state(thread_state)
            if until is None:
                ctx.logger.warning(
                    "Skipping snoozed thread with invalid until timestamp",
                    room_id=room_id,
                    thread_id=thread_root_id,
                )
                continue

            async def wake(
                *,
                _room_id: str = room_id,
                _thread_root_id: str = thread_root_id,
                _until: datetime = until,
            ) -> None:
                await _wake_thread(
                    room_id=_room_id,
                    thread_root_id=_thread_root_id,
                    expected_until=_until,
                    client=ctx.matrix_client,
                    store=ctx.thread_tag_store,
                    send_message=send_message,
                    logger=ctx.logger,
                )

            _spawn_snooze_task(
                room_id,
                thread_root_id,
                until,
                wake=wake,
                logger=ctx.logger,
            )


@hook(event="tool:after_call", name="thread-snooze-detect-tag", priority=100, timeout_ms=2000)
async def schedule_manual_snooze_tag(ctx: ToolAfterCallContext) -> None:
    """Track manual snooze tag changes from generic thread-tag tools."""
    manual_change = _manual_snooze_change(ctx)
    if manual_change is None:
        return
    payload, room_id, thread_root_id = manual_change

    if ctx.tool_name == "untag_thread":
        _cancel_snooze_task(room_id, thread_root_id)
        return

    until = _manual_snooze_tag_until(payload, ctx.arguments)
    if until is None:
        return
    if ctx.matrix_client is None or ctx.thread_tag_store is None:
        ctx.logger.warning(
            "Skipping snooze wake scheduling because runtime dependencies are unavailable",
            room_id=room_id,
            thread_id=thread_root_id,
        )
        return

    async def send_message(target_room_id: str, text: str, thread_id: str | None) -> str | None:
        return await ctx.send_message(target_room_id, text, thread_id=thread_id)

    async def wake() -> None:
        await _wake_thread(
            room_id=room_id,
            thread_root_id=thread_root_id,
            expected_until=until,
            client=ctx.matrix_client,
            store=ctx.thread_tag_store,
            send_message=send_message,
            logger=ctx.logger,
        )

    _spawn_snooze_task(room_id, thread_root_id, until, wake=wake, logger=ctx.logger)
