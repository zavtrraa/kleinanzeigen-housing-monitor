import asyncio
import json
import os
import time
from contextlib import suppress

from filelock import FileLock
from telegram import Bot
from telegram.error import Conflict

from ..core.eligibility import ListingFilter
from ..housing.models import HousingConfig
from ..housing.runner import build_sources
from ..housing.service import collect_sources
from ..housing.store import HousingStore
from ..housing.telegram import drain_outbox
from ..notifications.pacing import ChatPacer
from ..settings import Exclusions, load_yaml
from .configuration import preference_defaults
from .controls import handle_update
from .events import EventConsole
from .live_cycle import run_live_cycle
from .panel import ControlPanel
from .preferences import PreferenceStore
from .queue import RunQueue


async def run_daemon(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not settings.telegram_allowed_user_ids:
        raise ValueError("Set TELEGRAM_ALLOWED_USER_IDS before running --watch")
    if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_chat_id:
        raise ValueError("Telegram settings are required")
    with (
        FileLock(str(settings.data_dir / "runner.lock")).acquire(timeout=0),
        FileLock(str(settings.data_dir / "housing.lock")).acquire(timeout=0),
    ):
        queue = RunQueue(settings.data_dir / "runner.sqlite3", limit=settings.watch_queue_limit)
        preferences = PreferenceStore(queue, preference_defaults(settings))
        queue.set("schedule_interval", settings.watch_interval_seconds)
        queue.set("schedule_jitter", settings.watch_jitter_seconds)
        housing = HousingStore(settings.data_dir / "housing.sqlite3", recover=True)
        config = load_yaml(settings.housing_config_path, HousingConfig)
        filters = ListingFilter(load_yaml(settings.exclusions_path, Exclusions))
        sources = build_sources(settings, config, housing)
        stop = asyncio.Event()
        events = None
        tasks = []
        worker = None
        cancelled = False
        status_path = settings.data_dir / "runner-status.json"
        stop_path = settings.data_dir / "runner-stop.request"
        if stop_path.exists():
            stop_path.unlink()  # A fresh launch supersedes an old stop request.
        try:
            async with Bot(settings.telegram_bot_token.get_secret_value()) as real_bot:
                bot = ChatPacer(real_bot)
                chat = await bot.get_chat(settings.telegram_chat_id)
                chat_id = str(chat.id)
                panel = ControlPanel(queue, bot, chat_id)
                bot.on_message = panel.note_message
                if (await bot.get_webhook_info()).url:
                    raise ValueError("A webhook is configured; do not run a second update consumer")
                event_chat = settings.telegram_events_chat_id
                if event_chat:
                    try:
                        await bot.get_chat(event_chat)
                    except Exception as exc:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"Event channel unavailable: {type(exc).__name__}. "
                            "Monitoring continues; events are saved locally.",
                        )
                        event_chat = ""
                events = EventConsole(settings.data_dir, bot, event_chat)
                events.start()
                events.emit(
                    "Background monitor started",
                    "daemon.run_daemon",
                    "--watch",
                    f"pid={os.getpid()}",
                )
                await panel.refresh()
                template = settings.message_template_path.read_text(encoding="utf-8")

                async def work():
                    while not stop.is_set():
                        job = queue.claim()
                        if job is None:
                            await asyncio.sleep(0.5)
                            continue
                        events.emit(
                            f"Starting run #{job['id']}",
                            "queue.claim",
                            job["trigger"],
                            "run_live_cycle",
                        )
                        try:
                            report = await run_live_cycle(
                                settings,
                                bot=bot,
                                events=events,
                                trigger=job["trigger"],
                                request_id=str(job["id"]),
                                preferences=preferences.load(),
                            )
                            queue.complete(
                                job["id"],
                                cycle_id=report.id,
                                error="CycleHalted" if report.halted else None,
                            )
                            if report.halted:
                                queue.set("paused", True)
                                events.emit(
                                    "Queue paused after a stopped run",
                                    "worker.work",
                                    "--paused",
                                    report.id,
                                )
                        except asyncio.CancelledError:
                            queue.complete(job["id"], error="CancelledError")
                            raise
                        except Exception as exc:
                            queue.complete(job["id"], error=type(exc).__name__)
                            queue.set("paused", True)
                            events.emit(
                                "Run failed; queue paused",
                                "worker.work",
                                "--failed",
                                type(exc).__name__,
                            )

                async def schedule():
                    while not stop.is_set():
                        ident = queue.tick(
                            settings.watch_interval_seconds, settings.watch_jitter_seconds
                        )
                        if ident:
                            events.emit(
                                f"Scheduled run #{ident} queued",
                                "scheduler.tick",
                                "--50-70-minutes",
                                f"next_due={queue.get('next_due')}",
                            )
                        await asyncio.sleep(1)

                async def controls():
                    while not stop.is_set():
                        try:
                            offset = queue.telegram_offset()
                            updates = await bot.get_updates(
                                offset=offset,
                                timeout=5,
                                read_timeout=10,
                                allowed_updates=["message", "callback_query"],
                            )
                            for update in updates:
                                await handle_update(
                                    update,
                                    queue,
                                    bot,
                                    chat_id,
                                    settings.telegram_allowed_user_ids,
                                    events,
                                    housing,
                                    panel=panel,
                                    preferences=preferences,
                                    template=template,
                                )
                                queue.advance_telegram_offset(update.update_id)
                        except Conflict:
                            events.emit(
                                "Another process is polling Telegram. Stopping.",
                                "controls.poll",
                                "--conflict",
                                "getUpdates",
                            )
                            stop.set()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            events.emit(
                                "Telegram controls temporarily unavailable",
                                "controls.poll",
                                "--retry-in-10s",
                                type(exc).__name__,
                            )
                            await asyncio.sleep(10)

                async def public_sources():
                    while not stop.is_set():
                        if not queue.get("paused", False):
                            totals = await collect_sources(housing, sources, config, filters)
                            if totals:
                                events.emit(
                                    "Additional sources checked",
                                    "housing.collect_sources",
                                    "--notify-only",
                                    json.dumps(totals, ensure_ascii=False)[:400],
                                )
                            await drain_outbox(housing, bot, chat_id, template)
                        await asyncio.sleep(5)

                worker = asyncio.create_task(work())
                tasks = [
                    asyncio.create_task(schedule()),
                    asyncio.create_task(controls()),
                    asyncio.create_task(public_sources()),
                ]
                print(
                    f"Background monitor running. PID={os.getpid()}. "
                    "Telegram commands: /run /status /settings /pause /resume",
                    flush=True,
                )
                last_heartbeat_event = 0.0
                try:
                    while not stop.is_set():
                        if time.monotonic() - last_heartbeat_event >= 60:
                            events.emit(
                                "Worker heartbeat: "
                                + (
                                    "run active" if queue.status()["active"] else "waiting for work"
                                ),
                                "daemon.heartbeat",
                                "--watch",
                                "poll Telegram + check queue",
                            )
                            last_heartbeat_event = time.monotonic()
                        for task in tasks + [worker]:
                            if task.done():
                                task.result()
                                raise RuntimeError("Background loop exited unexpectedly")
                        payload = {
                            "pid": os.getpid(),
                            "state": "running",
                            "updated_at": time.time(),
                            **queue.status(),
                        }
                        temporary = status_path.with_suffix(".tmp")
                        temporary.write_text(
                            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                        )
                        temporary.replace(status_path)
                        if stop_path.exists():
                            stop_path.unlink()
                            stop.set()
                        await asyncio.sleep(1)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                finally:
                    stop.set()
                    for task in tasks:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                    if worker:
                        if cancelled:
                            worker.cancel()
                        with suppress(asyncio.CancelledError):
                            await worker
                    events.emit("Background monitor stopped", "daemon.run_daemon", "--stop", "")
                    await events.close()
        finally:
            status_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "state": "stopped",
                        "updated_at": time.time(),
                        **queue.status(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            housing.close()
            queue.close()
    return 0
