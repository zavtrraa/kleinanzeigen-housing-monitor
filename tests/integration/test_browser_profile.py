import importlib.util
import tempfile
import unittest
from pathlib import Path

from kleinanzeigen_bot.core.browser_audit import collect_browser_report
from kleinanzeigen_bot.core.browser_factory import BrowserFactory

CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


@unittest.skipUnless(
    importlib.util.find_spec("patchright") and CHROME.is_file(),
    "Optional Patchright and installed Chrome are required",
)
class NativeProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_patchright_chrome_native_values_are_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = BrowserFactory(
                Path(directory) / "audit",
                engine="patchright",
                channel="chrome",
                headless=True,
                timezone_id="Europe/Berlin",
            )
            async with factory.open() as context:
                report = await collect_browser_report(context)
            self.assertTrue(all(report["consistency"].values()), report["consistency"])
            self.assertFalse(report["automation_flag_exposed"])
            self.assertEqual(report["blocked_external_page_requests"], 0)
            self.assertEqual(report["main"]["timezone"], "Europe/Berlin")
            self.assertNotIn("cookie", report["headers"])
            self.assertNotIn("authorization", report["headers"])

    async def test_explicit_locale_is_rejected_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = BrowserFactory(
                Path(directory) / "audit",
                engine="patchright",
                channel="chrome",
                headless=True,
                locale="de-DE",
            )
            with self.assertRaisesRegex(ValueError, "native locale"):
                async with factory.open():
                    self.fail("Inconsistent locale override must not launch")
