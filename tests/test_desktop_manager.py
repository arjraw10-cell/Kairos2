import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kairos.desktop_manager import DesktopManager
from kairos.tools.desktop import DesktopDragTool, DesktopWindowActionTool


class DesktopManagerTests(unittest.TestCase):
    def test_decode_response_extracts_image_and_removes_base64_from_text(self):
        payload = {
            "content": [
                {"type": "text", "text": "window state"},
                {"type": "image", "data": "ZmFrZS1pbWFnZQ==", "mimeType": "image/png"},
            ],
            "structuredContent": {
                "pid": 42,
                "screenshot_png_b64": "ZmFrZS1pbWFnZQ==",
                "screenshot_mime_type": "image/png",
            },
        }
        output, image_url = DesktopManager._decode_response(json.dumps(payload))
        self.assertEqual(image_url, "data:image/png;base64,ZmFrZS1pbWFnZQ==")
        self.assertNotIn("ZmFrZS1pbWFnZQ==", output)
        self.assertIn('"pid": 42', output)

    def test_missing_driver_is_reported_as_tool_result_failure(self):
        manager = DesktopManager()
        with patch.object(manager, "_executable", return_value=None):
            result = manager.call("list_windows")
        self.assertFalse(result.success)
        self.assertIn("Cua Driver was not found", result.error or "")

    def test_driver_path_is_lazy_and_manager_can_be_constructed(self):
        # Desktop support must not probe or launch a native driver during Agent
        # construction. This also documents the intended manager lifecycle.
        manager = DesktopManager()
        self.assertFalse(manager.is_open)
        manager.close()
        self.assertFalse(manager.is_open)

    def test_daemon_stderr_goes_to_a_log_file_not_an_unread_pipe(self):
        launches = []

        class FakeDaemon:
            def __init__(self, args, **kwargs):
                launches.append(kwargs)
                kwargs["stderr"].write("driver: permission denied\n")
                kwargs["stderr"].flush()

            def poll(self):
                return 1  # exits during startup

        manager = DesktopManager()
        with tempfile.TemporaryDirectory() as root:
            manager.LOG_PATH = Path(root) / "logs" / "cua-driver.log"
            with patch.object(manager, "_executable", return_value="cua-driver"), \
                    patch.object(manager, "_status_ok", return_value=False), \
                    patch("kairos.desktop_manager.subprocess.Popen", FakeDaemon):
                with self.assertRaises(RuntimeError) as cm:
                    manager._ensure_daemon()
        self.assertNotIn(launches[0]["stderr"], (subprocess.PIPE, None))
        self.assertIn("permission denied", str(cm.exception))

    def test_drag_maps_public_coordinates_to_cua_fields(self):
        class FakeManager:
            def call(self, name, args):
                self.name, self.args = name, args
                return type("Result", (), {"success": True, "to_dict": lambda self: {}})()

        manager = FakeManager()
        DesktopDragTool(manager)(42, x1=1, y1=2, x2=3, y2=4)
        self.assertEqual(manager.name, "drag")
        self.assertEqual(manager.args["from_x"], 1)
        self.assertEqual(manager.args["from_y"], 2)
        self.assertEqual(manager.args["to_x"], 3)
        self.assertEqual(manager.args["to_y"], 4)

    def test_unsupported_window_actions_are_explicit(self):
        class FakeManager:
            def call(self, name, args):
                return type("Result", (), {"success": True})()

        result = DesktopWindowActionTool(FakeManager(), "close_window")(42)
        self.assertFalse(result.success)
        self.assertIn("does not provide", result.error or "")


if __name__ == "__main__":
    unittest.main()
