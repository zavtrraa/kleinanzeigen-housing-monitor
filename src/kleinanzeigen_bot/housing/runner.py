import asyncio
import json
from contextlib import suppress
from pathlib import Path

from filelock import FileLock
from telegram import Bot

from ..core.browser_factory import BrowserFactory
from ..core.eligibility import ListingFilter
from ..settings import Exclusions, load_yaml
from .email_sources import GmailSource, parse_alert
from .models import HousingConfig
from .policy import evaluate
from .public_sources import HanovaSource, HeimkehrSource
from .runtime import new_runtime, read_runtime, save_runtime
from .service import collect_sources
from .store import HousingStore
from .telegram import drain_outbox, poll_controls


def build_sources(settings, config, store):
    sources = []
    if config.heimkehr_enabled:
        sources.append(HeimkehrSource(config))
    if config.hanova_enabled:
        factory = BrowserFactory(
            settings.data_dir / "browser-profiles" / "hanova-public",
            headless=settings.headless,
            engine=settings.browser_engine,
            channel=settings.browser_channel,
            locale=settings.browser_locale,
            timezone_id=settings.browser_timezone,
            window_width=settings.browser_window_width,
            window_height=settings.browser_window_height,
            color_scheme=settings.browser_color_scheme,
        )
        sources.append(HanovaSource(config, factory))
    if config.gmail_enabled:
        if settings.gmail_address and settings.gmail_app_password.get_secret_value():
            sources.append(
                GmailSource(
                    config, settings.gmail_address, settings.gmail_app_password.get_secret_value()
                )
            )
        else:
            store.update_source(
                "gmail",
                status="needs_setup",
                next_poll=0,
                error="Configure GMAIL_ADDRESS and GMAIL_APP_PASSWORD; use the Housing folder",
            )
    return sources


async def run_housing(args, settings):
    config = load_yaml(settings.housing_config_path, HousingConfig)
    filters = ListingFilter(load_yaml(settings.exclusions_path, Exclusions))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if args.hub_status:
        store = HousingStore(settings.data_dir / "housing.sqlite3", recover=False)
        try:
            status = store.status()
            status["runtime"] = read_runtime(settings.data_dir)
            print(json.dumps(status, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    lock = FileLock(str(settings.data_dir / "housing.lock"))
    with lock.acquire(timeout=0):
        store = HousingStore(settings.data_dir / "housing.sqlite3", recover=True)
        try:
            if args.hub_retry_notifications:
                print(json.dumps({"requeued": store.retry_failed_notifications()}))
                return 0
            if args.hub_resume_source:
                name = args.hub_resume_source
                store.update_source(name, status="resumed", next_poll=0, paused=False)
                print("Source enabled for its next check: " + name)
                return 0
            if args.import_eml:
                raw = Path(args.import_eml).read_bytes()
                candidates = parse_alert(raw)
                for candidate in candidates:
                    store.upsert(
                        candidate, evaluate(candidate, config, filters), config.notify_review
                    )
                print(json.dumps({"imported": len(candidates), "outbox": store.status()["outbox"]}))
                return 0
            sources = build_sources(settings, config, store)
            if args.hub_collect:
                if store.get_meta("paused", False):
                    print("Housing hub is paused")
                    return 2
                totals = await collect_sources(store, sources, config, filters, force=True)
                print(
                    json.dumps(
                        {"sources": totals, "status": store.status(), "telegram_sent": False},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 2 if any("error" in value for value in totals.values()) else 0
            if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_chat_id:
                raise ValueError("Telegram credentials are required for --hub-watch / --hub-flush")
            template = settings.message_template_path.read_text(encoding="utf-8")
            async with Bot(token=settings.telegram_bot_token.get_secret_value()) as bot:
                if args.hub_flush:
                    sent = await drain_outbox(
                        store,
                        bot,
                        settings.telegram_chat_id,
                        template,
                        limit=20,
                    )
                    print(json.dumps({"sent": sent, "outbox": store.status()["outbox"]}))
                    return 0
                configured_chat = await bot.get_chat(settings.telegram_chat_id)
                control_chat_id = str(configured_chat.id)
                webhook = await bot.get_webhook_info()
                controls = not bool(webhook.url)

                runtime = new_runtime()
                runtime["controls_enabled"] = controls

                async def controls_loop():
                    while True:
                        try:
                            await poll_controls(
                                store,
                                bot,
                                control_chat_id,
                                settings.telegram_allowed_user_ids,
                            )
                        except Exception as exc:
                            runtime["control_error_type"] = type(exc).__name__
                            save_runtime(settings.data_dir, runtime)
                            await asyncio.sleep(10)

                control_task = asyncio.create_task(controls_loop()) if controls else None
                print(
                    f"Housing hub running. PID={runtime['pid']}. "
                    + (
                        "Telegram controls enabled."
                        if controls
                        else "Existing webhook preserved: controls disabled, notifications only."
                    )
                )
                runtime["phase"] = "starting"
                save_runtime(settings.data_dir, runtime)
                stop_reason = "normal"
                try:
                    while True:
                        if not store.get_meta("paused", False):
                            runtime["phase"] = "collecting"
                            save_runtime(settings.data_dir, runtime)
                            totals = await collect_sources(store, sources, config, filters)
                            if totals:
                                runtime["last_scan"] = totals
                                print(
                                    "Housing scan complete: "
                                    + json.dumps(totals, ensure_ascii=False),
                                    flush=True,
                                )
                            runtime["phase"] = "delivering"
                            save_runtime(settings.data_dir, runtime)
                            delivered = await drain_outbox(
                                store, bot, settings.telegram_chat_id, template
                            )
                            if delivered:
                                print(f"Telegram notifications delivered: {delivered}", flush=True)
                        runtime["phase"] = "paused" if store.get_meta("paused", False) else "idle"
                        save_runtime(settings.data_dir, runtime)
                        await asyncio.sleep(5)
                except asyncio.CancelledError:
                    stop_reason = "cancelled"
                    raise
                except BaseException as exc:
                    stop_reason = "error"
                    runtime["error_type"] = type(exc).__name__
                    raise
                finally:
                    if control_task:
                        control_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await control_task
                    runtime["state"] = "stopped"
                    runtime["stop_reason"] = stop_reason
                    save_runtime(settings.data_dir, runtime)
                    print(
                        f"Housing hub stopped. Reason={stop_reason}. "
                        "Details: data/hub-runtime.json",
                        flush=True,
                    )
        finally:
            store.close()
