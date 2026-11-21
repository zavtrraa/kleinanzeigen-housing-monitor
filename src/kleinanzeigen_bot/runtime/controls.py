from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from ..housing.telegram import process_update as housing_update
from .preferences import settings_text


def keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Run search", callback_data="r:run"),
                InlineKeyboardButton("Status", callback_data="r:status"),
            ],
            [
                InlineKeyboardButton("Search settings", callback_data="r:settings"),
                InlineKeyboardButton("Reply template", callback_data="r:template"),
            ],
            [
                InlineKeyboardButton("Pause", callback_data="r:pause"),
                InlineKeyboardButton("Resume", callback_data="r:resume"),
            ],
        ]
    )


def status_text(queue):
    state = queue.status()
    if queue.get("manual_only", False):
        return (
            "Housing monitor | Local demo\n"
            f"Active run: {state['active']['id'] if state['active'] else 'None'}"
            f"\nQueued: {state['jobs'].get('queued', 0)}"
            "\nManual runs only. Use Run search to start a synthetic check."
        )
    next_due = state["next_due"]
    next_text = (
        datetime.fromtimestamp(next_due, ZoneInfo("Europe/Berlin")).strftime("%d %b %Y %H:%M %Z")
        if next_due
        else "At startup"
    )
    active = state["active"]
    interval = queue.get("schedule_interval", 3600)
    jitter = queue.get("schedule_jitter", 600)
    return (
        "Housing monitor | "
        + ("Paused" if state["paused"] else "Running")
        + f"\nActive run: {active['id'] if active else 'None'}"
        + f"\nQueued: {state['jobs'].get('queued', 0)}"
        + f"\nNext scheduled run: {next_text}"
        + f"\nInterval: {(interval - jitter) / 60:g}-{(interval + jitter) / 60:g} minutes."
        + "\nManual requests wait for the active run to finish."
    )


async def acknowledge(query, text):
    """A stale Telegram callback must never prevent processing later updates."""
    try:
        await query.answer(text)
    except BadRequest as exc:
        message = str(exc).casefold()
        if "query is too old" not in message and "query id is invalid" not in message:
            raise


def _change_preferences(command, argument, preferences, update_id):
    fields = {"/city": "cities", "/search": "query", "/budget": "max_rent", "/area": "min_area"}
    if command == "/replies":
        if argument.lower() not in {"on", "off"}:
            raise ValueError("Use /replies on or /replies off")
        changes = {"auto_reply": argument.lower() == "on"}
    else:
        if not argument:
            raise ValueError(f"{command} needs a value")
        value = "" if command == "/search" and argument == "-" else argument
        changes = {fields[command]: value}
    changed = preferences.update(update_id=update_id, **changes)
    notice = "Settings saved for the next run." if changed else "This update was already handled."
    if command == "/replies" and preferences.load().auto_reply:
        notice += " Actual sending also requires DRY_RUN=false in the local configuration."
    return notice + "\n\n" + settings_text(preferences.load())


async def handle_update(
    update,
    queue,
    bot,
    chat_id,
    allowed_ids,
    events,
    housing=None,
    panel=None,
    *,
    preferences=None,
    template="",
):
    query = update.callback_query
    message = query.message if query else update.message
    actor = query.from_user if query else (message.from_user if message else None)
    if not message or str(message.chat_id) != str(chat_id) or actor is None:
        return
    if panel is not None and not query:
        panel.note_message(chat_id, getattr(message, "message_id", None))
    raw = (query.data or "") if query else (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    command = parts[0].split("@")[0].lower() if parts else ""
    argument = parts[1] if len(parts) > 1 else ""
    if command == "/whoami" and not query:
        await bot.send_message(chat_id=chat_id, text=f"Your Telegram user ID: {actor.id}")
        return
    if actor.id not in set(allowed_ids):
        if query:
            await acknowledge(query, "Your user ID is not authorized")
        return
    if query and command.startswith("h:") and housing is not None:
        await housing_update(update, housing, bot, chat_id, set(allowed_ids))
        return
    action = {
        "r:run": "run",
        "/run": "run",
        "r:status": "status",
        "/status": "status",
        "/start": "status",
        "/help": "status",
        "r:pause": "pause",
        "/pause": "pause",
        "r:resume": "resume",
        "/resume": "resume",
        "r:settings": "settings",
        "/settings": "settings",
        "r:template": "template",
        "/template": "template",
    }.get(command)
    if command in {"/city", "/search", "/budget", "/area", "/replies"}:
        action = "configure"
    if not action:
        if query:
            await acknowledge(query, "Unknown action")
        return
    if action == "run":
        try:
            ident, created = queue.request("manual", f"telegram:{update.update_id}")
            text = f"Run #{ident} queued."
            text += (
                " The active run will finish first."
                if queue.status()["active"]
                else " Starting shortly."
            )
            if not created:
                text = f"This request was already queued as run #{ident}."
            if queue.get("paused", False):
                text += " Monitoring is paused; use Resume to continue."
        except ValueError as exc:
            text = str(exc)
    elif action == "pause":
        queue.set("paused", True)
        text = "New runs paused. The active run will finish normally."
    elif action == "resume":
        queue.set("paused", False)
        text = "Queue resumed. Login and uncertain-delivery blocks still require manual review."
    elif action == "settings":
        text = (
            settings_text(preferences.load())
            if preferences
            else "Use the local search configuration."
        )
    elif action == "configure":
        if preferences is None:
            text = "Search settings are unavailable in this mode."
        else:
            try:
                text = _change_preferences(command, argument, preferences, update.update_id)
            except ValueError:
                text = (
                    "Invalid search setting. Use /city Hannover, /search Balkon, /budget 600, "
                    "/area 30 or /replies on|off. Rent must be between 0.01 and 10000; "
                    "area between 0 and 1000. Previous settings were kept."
                )
    elif action == "template":
        text = "Reply template\n\n" + (template or "No template configured.")
    else:
        text = status_text(queue)
    events.emit("Telegram command: " + action, "controls.handle_update", "--authorized", action)
    if query:
        await acknowledge(query, "Accepted")
    if panel is not None:
        await panel.refresh(
            text if action != "status" else "",
            clicked_id=getattr(message, "message_id", None) if query else None,
        )
    else:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard())
