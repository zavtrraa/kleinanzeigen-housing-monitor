import logging
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path

from filelock import FileLock, Timeout

from .exceptions import ProfileLocked

log = logging.getLogger(__name__)


class BrowserFactory:
    """Swappable engine; an optional async hook can configure the context."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        engine: str = "playwright",
        channel: str | None = None,
        locale: str | None = None,
        timezone_id: str | None = "Europe/Berlin",
        context_hook=None,
        window_width=1000,
        window_height=720,
        color_scheme="dark",
    ):
        self.profile_dir, self.headless = profile_dir, headless
        self.window_width, self.window_height = window_width, window_height
        self.color_scheme = color_scheme
        self.engine, self.context_hook = engine, context_hook
        self.channel, self.locale, self.timezone_id = channel, locale, timezone_id

    @asynccontextmanager
    async def open(self):
        if self.engine not in {"playwright", "patchright"}:
            raise ValueError("Unsupported browser engine")
        if self.engine == "patchright" and self.locale is not None:
            raise ValueError("Patchright profile requires native locale; remove BROWSER_LOCALE")
        if self.channel not in {None, "chrome", "msedge", "chromium"}:
            raise ValueError("Unsupported browser channel")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.profile_dir) + ".lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise ProfileLocked("Persistent browser profile is already in use") from exc
        try:
            api = import_module(f"{self.engine}.async_api")
            async with api.async_playwright() as driver:
                options = {
                    "headless": self.headless,
                    "no_viewport": True,
                    "accept_downloads": False,
                    "chromium_sandbox": True,
                    "color_scheme": self.color_scheme,
                    "args": [f"--window-size={self.window_width},{self.window_height}"]
                    + (["--force-dark-mode"] if self.color_scheme == "dark" else []),
                }
                if self.channel is not None:
                    options["channel"] = self.channel
                if self.locale is not None:
                    options["locale"] = self.locale
                if self.timezone_id is not None:
                    options["timezone_id"] = self.timezone_id
                context = await driver.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    **options,
                )
                try:
                    context.set_default_timeout(15000)
                    if not self.headless:
                        await self._fit_window(context)
                    if self.context_hook:
                        await self.context_hook(context)
                    yield context
                finally:
                    await context.close()
        finally:
            lock.release()

    async def _fit_window(self, context):
        pages = getattr(context, "pages", [])
        if not pages:
            return
        session = None
        try:
            size = await pages[0].evaluate("({width:screen.availWidth,height:screen.availHeight})")
            session = await context.new_cdp_session(pages[0])
            window = await session.send("Browser.getWindowForTarget")
            await session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "normal"}},
            )
            await session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {
                        "width": min(self.window_width, max(400, size["width"] - 60)),
                        "height": min(self.window_height, max(400, size["height"] - 60)),
                    },
                },
            )
        except Exception as exc:
            log.warning("window_resize_failed type=%s", type(exc).__name__)
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception as exc:
                    log.warning("window_session_detach_failed type=%s", type(exc).__name__)
