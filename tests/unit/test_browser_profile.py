import unittest

from kleinanzeigen_bot.core.browser_audit import assess_consistency


class BrowserProfileTests(unittest.TestCase):
    def test_inconsistent_worker_and_http_are_reported(self):
        data = {
            "main": {
                "userAgent": "Chrome/156.0",
                "platform": "Win32",
                "language": "de-DE",
                "languages": ["de-DE"],
                "hardwareConcurrency": 8,
                "timezone": "Europe/Berlin",
                "userAgentData": {
                    "platform": "Windows",
                    "brands": [
                        {"brand": "Chromium", "version": "156"},
                    ],
                },
            },
            "worker": {
                "userAgent": "Chrome/140.0",
                "platform": "Linux",
                "language": "en-US",
                "languages": ["en-US"],
                "hardwareConcurrency": 2,
                "timezone": "UTC",
            },
        }
        headers = {
            "user-agent": "Chrome/140.0",
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua-platform": '"Linux"',
        }
        result = assess_consistency(data, headers)
        self.assertFalse(result["worker_user_agent"])
        self.assertFalse(result["worker_platform"])
        self.assertFalse(result["http_user_agent"])
        self.assertFalse(result["http_language"])
        self.assertFalse(result["http_platform_hint"])
        self.assertTrue(result["user_agent_client_hint_version"])
