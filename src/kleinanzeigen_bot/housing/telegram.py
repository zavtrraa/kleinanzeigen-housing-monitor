import json

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter

from ..core.eligibility import warm_amounts
from .models import Candidate


def card(payload: dict, template: str):
    candidate = Candidate(**payload["candidate"])
    decision = payload["decision"]
    label = "Matches known criteria" if decision["outcome"] == "eligible" else "Review details"
    amounts = warm_amounts(candidate.description)
    warm = candidate.warm_eur or (
        str(next(iter(amounts))) if len(amounts) == 1 else "not specified"
    )
    text = "\n".join(
        filter(
            None,
            [
                f"{label} · {candidate.source}",
                candidate.title[:240],
                (candidate.address or candidate.city or "Confirm address")[:300],
                f"Warmmiete: {warm} € | area: {candidate.size_sqm or '?'} m² "
                f"| rooms: {candidate.rooms or '?'}",
                f"Available from: {candidate.available_from or 'confirm'}",
                "WBS: required" if candidate.wbs_required else "",
                *("• " + reason for reason in decision["reasons"]),
                "First seen or updated locally; publication date may differ.",
                candidate.url,
            ],
        )
    )
    rows = [[InlineKeyboardButton("View listing", url=candidate.url)]]
    if 1 <= len(template.strip()) <= 256:
        rows[0].append(
            InlineKeyboardButton("Reply template", copy_text=CopyTextButton(template.strip()))
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "Shortlist", callback_data=f"h:{candidate.short_id}:shortlisted"
                ),
                InlineKeyboardButton(
                    "Mark contacted", callback_data=f"h:{candidate.short_id}:contacted"
                ),
            ],
            [
                InlineKeyboardButton("Viewing", callback_data=f"h:{candidate.short_id}:viewing"),
                InlineKeyboardButton("Dismiss", callback_data=f"h:{candidate.short_id}:dismissed"),
            ],
        ]
    )
    return text, InlineKeyboardMarkup(rows)


async def drain_outbox(store, bot, chat_id: str, template: str, *, limit: int = 5, pace=None):
    import asyncio

    pace = pace or asyncio.sleep
    delivered = 0
    for _ in range(limit):
        if store.get_meta("paused", False):
            break
        event = store.claim_notification()
        if event is None:
            break
        payload = event["payload"]
        try:
            if "alert" in payload:
                text, keyboard = "Housing bot · " + payload["alert"], None
            else:
                text, keyboard = card(payload, template)
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=None,
                disable_web_page_preview=True,
            )
        except RetryAfter as exc:
            seconds = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else float(exc.retry_after)
            )
            store.delivery_failed(event, "RetryAfter", seconds + 1)
            break
        except (Forbidden, BadRequest) as exc:
            store.delivery_failed(event, type(exc).__name__, 0, fatal=True)
            break
        except Exception as exc:
            store.delivery_failed(
                event, type(exc).__name__, min(900, 30 * 2 ** (event["attempts"] - 1))
            )
            break
        else:
            store.delivered(event["id"], message.message_id)
            delivered += 1
            await pace(3.2)
    return delivered


def status_text(store) -> str:
    state = store.status()
    lines = [
        "Housing bot · " + ("paused" if state["paused"] else "running"),
        "Telegram queue: " + json.dumps(state["outbox"], ensure_ascii=False),
        "Matches: " + json.dumps(state["matches"], ensure_ascii=False),
        "Inventory (including exclusions): " + json.dumps(state["candidates"], ensure_ascii=False),
    ]
    for source in state["sources"]:
        lines.append(
            f"{source['name']}: {source['last_status'] or 'not checked'}"
            + (" | manual resume required" if source["paused"] else "")
            + (f" · {source['last_error']}" if source["last_error"] else "")
        )
    lines.append("Buttons track your actions; they do not send messages to landlords.")
    return "\n".join(lines)


async def _acknowledge(query, text):
    try:
        await query.answer(text)
    except BadRequest as exc:
        message = str(exc).casefold()
        if "query is too old" not in message and "query id is invalid" not in message:
            raise


async def process_update(update, store, bot, chat_id: str, allowed_ids: set[int]):
    query = update.callback_query
    message = query.message if query else update.message
    actor = query.from_user if query else (message.from_user if message else None)
    if not message or str(message.chat_id) != str(chat_id) or actor is None:
        if query:
            await _acknowledge(query, "Unavailable in this chat")
        return
    text = (message.text or "").strip() if not query else ""
    command = text.split()[0].split("@")[0].casefold() if text.startswith("/") else ""
    if not query and command == "/whoami":
        await bot.send_message(chat_id=chat_id, text=f"Your Telegram user ID: {actor.id}")
        return
    if actor.id not in allowed_ids:
        if query:
            await _acknowledge(query, "Only authorized users can change settings")
        elif command in {"/status", "/pause", "/resume", "/help"}:
            await bot.send_message(
                chat_id=chat_id,
                text="Add your ID to TELEGRAM_ALLOWED_USER_IDS locally. Find your ID with /whoami.",
            )
        return
    if query:
        parts = (query.data or "").split(":")
        workflows = {"shortlisted", "contacted", "viewing", "dismissed"}
        if len(parts) != 3 or parts[0] != "h" or parts[2] not in workflows:
            await _acknowledge(query, "Unknown action")
            return
        changed = store.set_workflow(parts[1], parts[2])
        await _acknowledge(query, "Marked: " + parts[2] if changed else "Listing not found")
    elif command == "/pause":
        store.set_meta("paused", True)
        await bot.send_message(chat_id=chat_id, text="Search and notification queue paused.")
    elif command == "/resume":
        store.set_meta("paused", False)
        await bot.send_message(
            chat_id=chat_id,
            text="Search resumed. Blocked sources remain paused.",
        )
    elif command == "/status":
        await bot.send_message(chat_id=chat_id, text=status_text(store))
    elif command == "/help":
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "/status · /pause · /resume · /whoami\n"
                "Buttons track actions; no automatic landlord messages."
            ),
        )


async def poll_controls(store, bot, chat_id, allowed_ids):
    offset = store.telegram_offset()
    updates = await bot.get_updates(
        offset=offset,
        timeout=5,
        read_timeout=10,
        allowed_updates=["message", "callback_query"],
    )
    for update in updates:
        await process_update(update, store, bot, chat_id, set(allowed_ids))
        store.advance_telegram_offset(update.update_id)
