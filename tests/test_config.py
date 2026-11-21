import unittest
from unittest.mock import patch

from app.config import Settings


class SettingsTests(unittest.TestCase):
    def test_direct_connection_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings()
        self.assertIsNone(settings.proxy_url)
        self.assertEqual(settings.database_url, "sqlite:///poker.db")
        self.assertEqual(settings.bot_token, "")

    def test_proxy_is_a_single_environment_string(self):
        with patch.dict("os.environ", {"PROXY_URL": " http://user:secret@proxy:3128 "}):
            settings = Settings()
        self.assertEqual(settings.proxy_url, "http://user:secret@proxy:3128")
        self.assertNotIn("secret", repr(settings))

    def test_blank_proxy_disables_proxy(self):
        with patch.dict("os.environ", {"PROXY_URL": "  "}):
            self.assertIsNone(Settings().proxy_url)


if __name__ == "__main__":
    unittest.main()
