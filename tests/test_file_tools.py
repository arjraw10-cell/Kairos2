import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kairos.tools.edit import EditTool
from kairos.tools.git import GitTool
from kairos.tools.read import MAX_IMAGE_BYTES, ReadTool
from kairos.tools.search import SearchTool
from kairos.tools.skills import SkillManager
from kairos.tools.write import WriteTool

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
CONFIG = {
    "default_setup": "fake",
    "providers": {"local": {"format": "completions", "base_url": "http://127.0.0.1:9/v1", "api_keys": ["k"]}},
    "setups": {"fake": {"provider": "local", "model": "fake-model"}},
}


class TempWorkspace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name).resolve()
        # The process CWD is deliberately not the workspace.
        self.assertNotEqual(Path.cwd().resolve(), self.ws)


class WorkspacePathTests(TempWorkspace):
    def test_relative_paths_resolve_against_the_workspace(self):
        (self.ws / "kairos_ws_probe.txt").write_text("hello workspace\n", encoding="utf-8")

        result = ReadTool(str(self.ws))("kairos_ws_probe.txt")
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, "hello workspace\n")

        self.assertTrue(WriteTool(str(self.ws))("sub/new.txt", "x").success)
        self.assertEqual((self.ws / "sub" / "new.txt").read_bytes(), b"x")
        self.assertFalse(Path("sub/new.txt").exists())

        self.assertTrue(EditTool(str(self.ws))("kairos_ws_probe.txt", "hello", "bye").success)
        self.assertEqual((self.ws / "kairos_ws_probe.txt").read_text(encoding="utf-8"), "bye workspace\n")

    def test_search_defaults_to_the_workspace(self):
        (self.ws / "sub").mkdir()
        (self.ws / "sub" / "f.txt").write_text("zq_unique_needle_7\n", encoding="utf-8")
        tool = SearchTool(str(self.ws))
        result = tool("zq_unique_needle_7")
        self.assertTrue(result.success)
        self.assertIn("1 matches", result.output)
        self.assertIn("1 matches", tool("zq_unique_needle_7", path="sub").output)

    def test_agent_tools_and_terminals_use_the_workspace(self):
        from kairos.agent import Agent

        path = self.ws / "provider.json"
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        patcher = mock.patch("kairos.providers.config.config_path", return_value=path)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.ws / "rel.txt").write_text("in workspace", encoding="utf-8")

        agent = Agent(str(self.ws))
        result = json.loads(agent._execute_tool("read", {"path": "rel.txt"}))
        self.assertEqual(result["output"], "in workspace")

        tid = agent.terminal_manager.create_terminal(background=False)
        ok, out = agent.terminal_manager.execute_command(
            tid, "cd" if os.name == "nt" else "pwd", timeout=10
        )
        self.assertTrue(ok, out)
        self.assertEqual(Path(out.strip().splitlines()[-1]).resolve(), self.ws)
        # SkillManager no longer creates <workspace>/skills for every Agent.
        self.assertFalse((self.ws / "skills").exists())


class EditTests(TempWorkspace):
    def test_crlf_file_keeps_crlf(self):
        f = self.ws / "crlf.txt"
        f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        result = EditTool()(str(f), "one\ntwo", "uno\ndos")
        self.assertTrue(result.success, result.error)
        self.assertEqual(f.read_bytes(), b"uno\r\ndos\r\nthree\r\n")

    def test_lf_file_keeps_lf(self):
        f = self.ws / "lf.txt"
        f.write_bytes(b"one\ntwo\nthree\n")
        result = EditTool()(str(f), "two", "2\n2b")
        self.assertTrue(result.success, result.error)
        self.assertEqual(f.read_bytes(), b"one\n2\n2b\nthree\n")

    def test_non_utf8_file_is_refused_and_unchanged(self):
        f = self.ws / "cp1252.txt"
        f.write_bytes("café\n".encode("cp1252"))
        result = EditTool()(str(f), "caf", "CAF")
        self.assertFalse(result.success)
        self.assertIn("UTF-8", result.error)
        self.assertEqual(f.read_bytes(), b"caf\xe9\n")

    def test_empty_old_text_is_rejected(self):
        f = self.ws / "a.txt"
        f.write_bytes(b"abc\n" * 100)
        result = EditTool()(str(f), "", "x")
        self.assertFalse(result.success)
        self.assertIn("empty", result.error)
        self.assertEqual(f.read_bytes(), b"abc\n" * 100)

    def test_many_matches_report_is_bounded(self):
        f = self.ws / "big.txt"
        f.write_bytes(b"a\n" * 50_000)
        start = time.monotonic()
        result = EditTool()(str(f), "a", "b")
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(result.success)
        self.assertIn("50000 occurrences", result.error)
        self.assertLess(len(result.error), 500)


class WriteTests(TempWorkspace):
    def test_writes_content_exactly(self):
        f = self.ws / "w.txt"
        self.assertTrue(WriteTool()(str(f), "a\nb\r\nc").success)
        self.assertEqual(f.read_bytes(), b"a\nb\r\nc")


class ReadImageTests(TempWorkspace):
    def test_png_is_returned_as_image(self):
        f = self.ws / "p.png"
        f.write_bytes(PNG)
        result = ReadTool()(str(f))
        self.assertTrue(result.success)
        self.assertTrue(result.image_url.startswith("data:image/png;base64,"))

    def test_mime_comes_from_content_not_extension(self):
        f = self.ws / "actually_jpeg.png"
        f.write_bytes(JPEG)
        result = ReadTool()(str(f))
        self.assertTrue(result.image_url.startswith("data:image/jpeg;base64,"))

    def test_unsupported_formats_are_refused(self):
        for name, data in (("b.bmp", b"BM" + b"\x00" * 40), ("fake.png", b"not an image"),
                           ("t.tiff", b"II*\x00" + b"\x00" * 20)):
            f = self.ws / name
            f.write_bytes(data)
            result = ReadTool()(str(f))
            self.assertFalse(result.success, name)
            self.assertIsNone(result.image_url)
            self.assertIn("PNG, JPEG, GIF and WebP", result.error)

    def test_svg_is_read_as_text(self):
        f = self.ws / "i.svg"
        f.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        result = ReadTool()(str(f))
        self.assertTrue(result.success)
        self.assertIsNone(result.image_url)
        self.assertIn("<svg", result.output)

    def test_image_over_the_provider_limit_is_refused(self):
        f = self.ws / "big.png"
        f.write_bytes(PNG + b"\x00" * (MAX_IMAGE_BYTES + 1 - len(PNG)))
        result = ReadTool()(str(f))
        self.assertFalse(result.success)
        self.assertIn("too large", result.error)
        # Anthropic caps the base64 data at 5 MB.
        self.assertLessEqual((MAX_IMAGE_BYTES + 2) // 3 * 4, 5 * 1024 * 1024)


class SearchTimeoutTests(TempWorkspace):
    def test_catastrophic_regex_honours_the_timeout(self):
        (self.ws / "f.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
        start = time.monotonic()
        result = SearchTool(str(self.ws))(r"(a|aa)+$", timeout=1)
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(result.success)
        self.assertIn("timed out", result.error)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitEncodingTests(TempWorkspace):
    def test_utf8_diff_is_decoded(self):
        def git(*args):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                cwd=self.ws, check=True, capture_output=True, stdin=subprocess.DEVNULL,
            )

        git("init", "-q")
        (self.ws / "f.txt").write_bytes(b"x\n")
        git("add", "f.txt")
        git("commit", "-q", "-m", "init")
        (self.ws / "f.txt").write_bytes("Łódź\n".encode("utf-8"))
        tool = GitTool()
        tool.set_workspace(str(self.ws))
        result = tool("diff")
        self.assertTrue(result.success, result.error)
        self.assertIn("+Łódź", result.output)


class SkillManagerTests(TempWorkspace):
    def test_skills_folder_is_created_only_when_writing(self):
        manager = SkillManager(str(self.ws / "skills"))
        self.assertFalse((self.ws / "skills").exists())
        self.assertEqual(manager.list_skills().output, "No skills available.")
        self.assertTrue(manager.write_skill("demo", "# Demo\n").success)
        self.assertTrue((self.ws / "skills" / "demo" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
