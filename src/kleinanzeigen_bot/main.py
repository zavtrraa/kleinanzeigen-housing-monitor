import argparse
import asyncio
import json
import sys
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Kleinanzeigen apartment assistant")
    modes = cli.add_mutually_exclusive_group(required=True)
    modes.add_argument("--demo", action="store_true", help="Offline dry run, no credentials needed")
    modes.add_argument(
        "--demo-ui", action="store_true", help="Interactive local demo with fictional data"
    )
    modes.add_argument(
        "--login", action="store_true", help="Open profile for manual login/challenge"
    )
    modes.add_argument("--once", action="store_true", help="Run one cycle")
    modes.add_argument("--watch", action="store_true", help="Hourly queue and Telegram controls")
    modes.add_argument(
        "--send-test",
        action="store_true",
        help="Isolated sending test; no account or external writes",
    )
    modes.add_argument(
        "--telegram-test",
        action="store_true",
        help="Send one explicit test message to the configured Telegram chat",
    )
    modes.add_argument(
        "--telegram-check",
        action="store_true",
        help="Check Telegram bot/chat access without sending messages or consuming updates",
    )
    modes.add_argument(
        "--browser-check",
        action="store_true",
        help="Audit browser consistency on an intercepted local page, without accessing the site",
    )
    modes.add_argument(
        "--check", action="store_true", help="Validate config without browser/network"
    )
    modes.add_argument("--status", action="store_true", help="Show pause and uncertain listings")
    modes.add_argument(
        "--history", metavar="LISTING_ID", help="Show local observation micro-patches"
    )
    modes.add_argument(
        "--resume", action="store_true", help="Clear pause after manual verification"
    )
    modes.add_argument(
        "--resolve", metavar="LISTING_ID", help="Manually resolve an uncertain listing"
    )
    modes.add_argument(
        "--hub-collect",
        action="store_true",
        help="Collect new sources; queue only, never send Telegram",
    )
    modes.add_argument(
        "--hub-watch", action="store_true", help="Run multi-source search and Telegram"
    )
    modes.add_argument(
        "--hub-flush",
        action="store_true",
        help="Send queued housing notifications to configured Telegram chat",
    )
    modes.add_argument(
        "--hub-retry-notifications",
        action="store_true",
        help="Explicitly requeue failed Telegram notifications after fixing delivery",
    )
    modes.add_argument("--hub-status", action="store_true", help="Read multi-source status")
    modes.add_argument(
        "--hub-resume-source",
        choices=["hanova", "heimkehr", "gmail"],
        help="Explicitly resume a source after manual verification",
    )
    modes.add_argument("--import-eml", type=Path, help="Import one saved alert email, queue only")
    cli.add_argument("--as", dest="resolution", choices=["sent", "discovered"])
    cli.add_argument("--reason", help="Required audit reason for manual resolution")
    cli.add_argument("--data-dir", type=Path, help="Override state directory")
    cli.add_argument("--port", type=int, default=8765, help="Local demo port (default: 8765)")
    return cli


async def run(args) -> int:
    if args.demo_ui:
        from .demo.server import serve_demo

        await serve_demo(args.data_dir or Path("data/demo"), args.port)
        return 0

    if args.demo:
        from .demo import run_demo

        report = await run_demo(args.data_dir or Path("data"))
        return 2 if report.halted or report.errors else 0

    from filelock import FileLock, Timeout

    from .core.exceptions import IPRangeBlocked, ProfileLocked
    from .observability.logging import configure_logging
    from .settings import Settings, Variants, load_yaml
    from .state.sqlite_repo import SQLiteRepository

    configure_logging()
    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir
    settings.data_dir = settings.data_dir.resolve()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    if args.send_test:
        from .runtime.send_probe import run_send_probe

        return await run_send_probe(settings)
    if args.watch:
        from .runtime.daemon import run_daemon

        return await run_daemon(settings)
    if args.once:
        from .runtime.live_cycle import run_live_cycle

        report = await run_live_cycle(settings)
        return 2 if report.halted or report.errors else 0

    if any(
        (
            args.hub_collect,
            args.hub_watch,
            args.hub_flush,
            args.hub_status,
            args.hub_retry_notifications,
            args.hub_resume_source,
            args.import_eml,
        )
    ):
        from .housing.runner import run_housing

        return await run_housing(args, settings)

    if args.telegram_test:
        if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_chat_id:
            raise ValueError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
        from telegram import Bot

        from .notifications.telegram_bot import send_connection_test

        async with Bot(token=settings.telegram_bot_token.get_secret_value()) as bot:
            result = await send_connection_test(bot, settings.telegram_chat_id)
        print(json.dumps(result))
        return 0

    if args.telegram_check:
        if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_chat_id:
            raise ValueError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
        from telegram import Bot

        from .notifications.telegram_bot import inspect_connection

        async with Bot(token=settings.telegram_bot_token.get_secret_value()) as bot:
            report = await inspect_connection(bot, settings.telegram_chat_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.check:
        from .runtime.configuration import resolve_configuration

        configuration = resolve_configuration(settings)
        variants = load_yaml(settings.message_variants_path, Variants)
        from .action.text_variator import TextVariator

        TextVariator(
            settings.message_template_path.read_text(encoding="utf-8"),
            variants.greetings,
            variants.closings,
            settings.signature,
        )
        print(
            "Configuration valid. "
            f"Mode: {configuration.mode}; form validation: {configuration.runtime.live_validation}."
        )
        return 0

    lock = FileLock(str(settings.data_dir / "bot.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise ProfileLocked("Another bot/login process owns this data directory") from exc
    try:
        repo = SQLiteRepository(settings.data_dir / "state.sqlite3")
        try:
            recovered = repo.recover_interrupted()
            if recovered:
                repo.pause(f"{recovered} interrupted attempt(s); inspect --status before --resume")
            if args.history:
                print(json.dumps(repo.history(args.history), ensure_ascii=False, indent=2))
                return 0
            if args.status:
                print(
                    json.dumps(
                        {"paused": repo.pause_reason(), "uncertain": repo.uncertain()},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.resolve:
                if not args.resolution or not args.reason:
                    raise ValueError("--resolve requires --as and --reason")
                repo.resolve(args.resolve, args.resolution, args.reason)
                print(
                    "Resolution recorded. Use --resume separately after checking remaining items."
                )
                return 0
            if args.resume:
                if repo.uncertain():
                    raise ValueError("Resolve every uncertain listing before resuming")
                repo.resume()
                print("Pause cleared. Start --once to verify the next cycle.")
                return 0

            from .core.browser_factory import BrowserFactory

            profile_key = f"{settings.browser_engine}-{settings.browser_channel or 'bundled'}"
            profile_group = "audit-profiles" if args.browser_check else "browser-profiles"
            factory = BrowserFactory(
                settings.data_dir / profile_group / profile_key,
                headless=False if args.login else settings.headless,
                engine=settings.browser_engine,
                channel=settings.browser_channel,
                locale=settings.browser_locale,
                timezone_id=settings.browser_timezone,
                window_width=settings.browser_window_width,
                window_height=settings.browser_window_height,
                color_scheme=settings.browser_color_scheme,
            )
            if args.browser_check:
                from .core.browser_audit import collect_browser_report

                async with factory.open() as context:
                    report = await collect_browser_report(context)
                report["configuration"] = {
                    "engine": settings.browser_engine,
                    "channel": settings.browser_channel,
                    "headless": settings.headless,
                    "locale": settings.browser_locale or "native",
                    "timezone": settings.browser_timezone or "native",
                }
                destination = settings.data_dir / "browser-check.json"
                temporary = destination.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                temporary.replace(destination)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                print(f"Local browser report saved: {destination}")
                return 0 if all(report["consistency"].values()) else 2
            if args.login:
                from .core.ui import ensure_not_ip_blocked

                try:
                    async with factory.open() as context:
                        page = await context.new_page()
                        await page.goto(
                            "https://www.kleinanzeigen.de", wait_until="domcontentloaded"
                        )
                        await ensure_not_ip_blocked(page)
                        print("Complete login/challenge manually. Press Enter here when finished.")
                        await asyncio.to_thread(input)
                        for opened_page in context.pages:
                            await ensure_not_ip_blocked(opened_page)
                except IPRangeBlocked:
                    repo.pause("IPRangeBlocked")
                    print("IP range temporarily blocked. Login aborted; durable pause saved.")
                    return 2
                print("Browser profile saved.")
                if repo.pause_reason():
                    print(
                        "A saved pause remains. Inspect --status; use --resume after verification."
                    )
                return 0

            raise ValueError("Choose an explicit mode; see --help")
        finally:
            repo.close()
    finally:
        lock.release()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    cli = parser()
    args = cli.parse_args()
    if not args.resolve and (args.resolution or args.reason):
        cli.error("--as and --reason can only be used with --resolve")
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Stopped by keyboard interrupt (exit 130).", file=sys.stderr)
        code = 130
    except ModuleNotFoundError as exc:
        import re

        package = exc.name or "unknown"
        package = package if re.fullmatch(r"[A-Za-z0-9_.-]+", package) else "unknown"
        print(
            f"Missing dependency: {package}. Use run-local.ps1 or install "
            'the project into this interpreter: python -m pip install -e ".[dev,browser]"',
            file=sys.stderr,
        )
        code = 1
    except Exception as exc:
        # Config value errors are actionable; validation and HTTP/browser errors are sanitized.
        from .core.exceptions import BotError

        if type(exc) is ValueError or isinstance(exc, BotError):
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            print(
                f"Startup failed: {type(exc).__name__}. Check config, dependencies and profile.",
                file=sys.stderr,
            )
        code = 1
    raise SystemExit(code)
