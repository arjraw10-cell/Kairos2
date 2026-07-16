import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kairos.tools.session import SessionManager


class SessionStorageTests(unittest.TestCase):
    def test_workspace_isolated_home_store_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            workspace_a = Path(temp) / "one" / "App"
            workspace_b = Path(temp) / "two" / "App"
            workspace_a.mkdir(parents=True)
            workspace_b.mkdir(parents=True)
            history = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]

            with patch("kairos.tools.session.Path.home", return_value=home):
                manager_a = SessionManager(workspace_a)
                manager_b = SessionManager(workspace_b)
                manager_a.save_chat(history)

                self.assertEqual(manager_a.list_sessions()[0]["preview"], "hello")
                self.assertEqual(manager_b.list_sessions(), [])
                self.assertIn("App--", str(manager_a._chat_file()))
                saved = json.loads(manager_a._chat_file().read_text(encoding="utf-8"))
                self.assertEqual(saved["workspace"], str(workspace_a.resolve()))
                self.assertIn("sessions", saved)
                self.assertNotEqual(manager_a._chat_file(), manager_b._chat_file())

    def test_workspace_local_legacy_chats_are_loadable_and_migrate_on_save(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            workspace = Path(temp) / "project"
            legacy_file = workspace / "chats" / "chats.json"
            legacy_file.parent.mkdir(parents=True)
            legacy = {
                "chat_legacy": {
                    "timestamp": "2024-01-01 00:00:00",
                    "preview": "old chat",
                    "messages": [
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "old chat"},
                    ],
                }
            }
            legacy_file.write_text(json.dumps(legacy), encoding="utf-8")

            with patch("kairos.tools.session.Path.home", return_value=home):
                manager = SessionManager(workspace)
                self.assertEqual(manager.load_session("chat_legacy")[1]["content"], "old chat")
                self.assertEqual(manager.list_sessions()[0]["id"], "chat_legacy")
                manager.save_chat(manager.load_session("chat_legacy"))

                self.assertTrue(manager._chat_file().exists())
                saved = json.loads(manager._chat_file().read_text(encoding="utf-8"))
                self.assertIn("chat_legacy", saved["sessions"])
                self.assertTrue(legacy_file.exists())


if __name__ == "__main__":
    unittest.main()
