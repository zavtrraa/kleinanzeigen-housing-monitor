from contextlib import AsyncExitStack
from datetime import UTC, datetime
from uuid import uuid4

from filelock import FileLock
from telegram import Bot

from ..action.humanizer import between_messages
from ..action.sender import PlaywrightMessageSender
from ..action.text_variator import TextVariator
from ..core.browser_factory import BrowserFactory
from ..core.models import CycleReport
from ..core.monitor import Monitor
from ..core.orchestrator import Orchestrator
from ..ingestion.details import ListingInspector
from ..ingestion.search import PlaywrightSearchClient
from ..notifications.pacing import ChatPacer
from ..notifications.telegram_bot import ConsoleNotifier, TelegramNotifier
from ..settings import Variants, load_yaml
from ..state.observations import digest
from ..state.sqlite_repo import SQLiteRepository
from .configuration import preference_defaults, resolve_configuration
from .events import EventConsole
from .preferences import read_preferences


async def run_live_cycle(
    settings,
    *,
    bot=None,
    events=None,
    trigger="once",
    request_id="",
    keyboard=None,
    preferences=None,
):
    invoked_at = datetime.now(UTC).isoformat()
    report = None
    mode = "DRY RUN" if settings.dry_run else "LIVE"
    print(f"Run #{request_id or 'once'} · {mode} · {trigger}", flush=True)
    async with AsyncExitStack() as stack:
        if bot is None and settings.telegram_bot_token.get_secret_value():
            bot = ChatPacer(
                await stack.enter_async_context(Bot(settings.telegram_bot_token.get_secret_value()))
            )
        if events is None:
            events = EventConsole(settings.data_dir, bot, settings.telegram_events_chat_id)
            events.start()
            stack.push_async_callback(events.close)
        notifier = (
            TelegramNotifier(
                bot, settings.telegram_chat_id, console=True, rich=True, keyboard=keyboard
            )
            if bot is not None
            else ConsoleNotifier()
        )
        events.emit("Run accepted", "run_live_cycle", f"--{trigger}", f"request #{request_id}")
        try:
            with FileLock(str(settings.data_dir / "bot.lock")).acquire(timeout=0):
                repo = SQLiteRepository(settings.data_dir / "state.sqlite3")
                try:
                    recovered = repo.recover_interrupted()
                    if recovered:
                        repo.pause(
                            f"{recovered} interrupted attempt(s); inspect --status before --resume"
                        )
                    preferences = preferences or read_preferences(
                        settings.data_dir / "runner.sqlite3", preference_defaults(settings)
                    )
                    configuration = resolve_configuration(settings, preferences)
                    preferences = configuration.preferences
                    profiles = configuration.profiles
                    runtime = configuration.runtime
                    variants = load_yaml(settings.message_variants_path, Variants)
                    exclusions = configuration.exclusions
                    filters = configuration.filters
                    repo.check_policy = digest(
                        {
                            "runtime": runtime.model_dump(mode="json"),
                            "engine": "0.7.0",
                            "exclusions": exclusions.model_dump(mode="json"),
                            "allow_unpriced": settings.allow_unpriced_replies,
                            "preferences": preferences.model_dump(mode="json"),
                        }
                    )
                    texts = TextVariator(
                        settings.message_template_path.read_text(encoding="utf-8"),
                        variants.greetings,
                        variants.closings,
                        settings.signature,
                    )

                    async def pace():
                        await between_messages(
                            settings.min_listing_pause_seconds,
                            settings.max_listing_pause_seconds,
                            progress=lambda message: events.emit(
                                message,
                                "pacing.wait",
                                f"--pause={settings.min_listing_pause_seconds}-{settings.max_listing_pause_seconds}s",
                            ),
                        )

                    async def between():
                        await between_messages(
                            settings.min_message_pause_seconds, settings.max_message_pause_seconds
                        )

                    factory = BrowserFactory(
                        settings.data_dir
                        / "browser-profiles"
                        / f"{settings.browser_engine}-{settings.browser_channel or 'bundled'}",
                        headless=settings.headless,
                        engine=settings.browser_engine,
                        channel=settings.browser_channel,
                        locale=settings.browser_locale,
                        timezone_id=settings.browser_timezone,
                        window_width=settings.browser_window_width,
                        window_height=settings.browser_window_height,
                        color_scheme=settings.browser_color_scheme,
                    )
                    # Report a saved operational pause even when Chrome cannot launch.
                    if repo.pause_reason():
                        report = CycleReport(
                            "paused-" + request_id,
                            settings.dry_run,
                            halted=True,
                            errors=[repo.pause_reason()],
                            trigger=trigger,
                            request_id=request_id,
                        )
                        report.finished_at = report.started_at
                        await notifier.summary(report)
                        return report
                    events.emit(
                        "Opening saved browser profile",
                        "BrowserFactory.open",
                        f"--{settings.browser_engine}",
                        settings.browser_channel or "bundled",
                    )
                    async with factory.open() as context:
                        search = PlaywrightSearchClient(
                            context,
                            profiles,
                            runtime,
                            settings.data_dir / "diagnostics",
                            pause_between_profiles=pace,
                            events=events,
                        )
                        if not preferences.auto_reply:
                            runner = Monitor(
                                search,
                                ListingInspector(
                                    context, runtime, settings.data_dir / "diagnostics", pace
                                ),
                                repo,
                                notifier,
                                preferences,
                                filters,
                                destination=settings.telegram_chat_id or "console",
                                trigger=trigger,
                                request_id=request_id,
                                recovered=recovered,
                            )
                            report = await runner.run_cycle()
                            return report
                        runner = Orchestrator(
                            search,
                            repo,
                            PlaywrightMessageSender(
                                context,
                                runtime,
                                filters,
                                settings.data_dir / "diagnostics",
                                pace,
                                allow_unpriced=settings.allow_unpriced_replies,
                                events=events,
                                details_observer=repo.record_details,
                                preferences=preferences,
                            ),
                            texts,
                            notifier,
                            dry_run=settings.dry_run,
                            maximum=settings.max_messages_per_cycle,
                            pause_between=between,
                            recovered=recovered,
                            progress=lambda text: print(text, flush=True),
                            trigger=trigger,
                            request_id=request_id,
                            events=events,
                            started_at=invoked_at,
                            fresh_first=settings.fresh_first,
                            excluded_recheck_seconds=settings.excluded_recheck_seconds,
                            prepare_retry_seconds=settings.prepare_retry_seconds,
                            prepare_failure_limit=settings.prepare_failure_limit,
                        )
                        report = await runner.run_cycle()
                        return report
                finally:
                    repo.close()
        except Exception as exc:
            events.emit(
                "Run failed; see error type",
                "run_live_cycle",
                "--failed",
                type(exc).__name__,
            )
            if report is None:
                report = CycleReport(
                    str(uuid4()),
                    settings.dry_run,
                    halted=True,
                    started_at=invoked_at,
                    finished_at=datetime.now(UTC).isoformat(),
                    errors=[f"Startup or cycle error: {type(exc).__name__}"],
                    trigger=trigger,
                    request_id=request_id,
                )
            else:
                report.halted = True
                report.errors.append(f"Browser shutdown error: {type(exc).__name__}")
            await notifier.summary(report)
            raise
