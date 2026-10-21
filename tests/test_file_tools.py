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

    def make_agent(self):
        """A real Agent on the workspace, with a fake provider.json."""
        from kairos.agent import Agent

        path = self.ws / "provider.json"
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        patcher = mock.patch("kairos.providers.config.config_path", return_value=path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Agent(str(self.ws))


class WorkspacePathTests(TempWorkspace):
    def test_relative_paths_resolve_against_the_workspace(self):
        (self.ws / "kairos_ws_probe.txt").write_text("hello workspace\n", encoding="utf-8")

        result = ReadTool(str(self.ws))("kairos_ws_probe.txt")
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, "     1\thello workspace\n[End of file — 1 lines]")

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
        self.assertEqual(result["output"], "     1\tin workspace\n[End of file — 1 lines]")

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
        self.assertIn("Found 50000 matches", result.error)
        self.assertIn("replace_all=true", result.error)
        self.assertLess(len(result.error), 500)

    def test_smart_quotes_dashes_and_spaces_match_their_ascii_forms(self):
        f = self.ws / "q.py"
        f.write_bytes("a = 1\nmsg = “it’s — done” # note\nb = 2\n".encode("utf-8"))
        result = EditTool()(str(f), "msg = \"it's - done\" # note", "msg = 'ok'")
        self.assertTrue(result.success, result.error)
        self.assertIn("normalizing", result.output)
        self.assertEqual(f.read_bytes(), b"a = 1\nmsg = 'ok'\nb = 2\n")

    def test_exact_match_wins_over_normalized_ones(self):
        f = self.ws / "q.txt"
        f.write_bytes("x = 'a'\ny = ‘a’\n".encode("utf-8"))
        result = EditTool()(str(f), "'a'", "'b'")
        self.assertTrue(result.success, result.error)
        self.assertEqual(f.read_bytes(), "x = 'b'\ny = ‘a’\n".encode("utf-8"))

    def test_trailing_whitespace_is_ignored_and_kept_outside_the_match(self):
        f = self.ws / "t.py"
        f.write_bytes(b"def f():  \n    return 1\t\n\nkeep = 1   \n")
        result = EditTool()(str(f), "def f():\n    return 1", "def g():\n    return 2")
        self.assertTrue(result.success, result.error)
        self.assertIn("trailing whitespace", result.output)
        self.assertEqual(f.read_bytes(), b"def g():\n    return 2\t\n\nkeep = 1   \n")

    def test_trailing_whitespace_tier_keeps_crlf_and_bom(self):
        f = self.ws / "crlf_bom.txt"
        f.write_bytes(b"\xef\xbb\xbfone \r\ntwo\r\nthree\r\n")
        result = EditTool()(str(f), "one\ntwo\n", "uno\ndos\n")
        self.assertTrue(result.success, result.error)
        self.assertEqual(f.read_bytes(), b"\xef\xbb\xbfuno\r\ndos\r\nthree\r\n")

    def test_line_number_prefixes_from_read_are_removed(self):
        f = self.ws / "n.py"
        f.write_bytes(b"a = 1\nb = 2\nc = 3\n")
        result = EditTool()(str(f), "     2\tb = 2\n     3\tc = 3", "     2\tb = 20\n     3\tc = 30")
        self.assertTrue(result.success, result.error)
        self.assertIn("line-number prefixes", result.output)
        self.assertEqual(f.read_bytes(), b"a = 1\nb = 20\nc = 30\n")

    def test_line_numbers_are_kept_unless_every_line_has_one(self):
        f = self.ws / "n.txt"
        f.write_bytes(b"x\n")
        result = EditTool()(str(f), "     1\tx\ny", "z")
        self.assertFalse(result.success)
        self.assertEqual(f.read_bytes(), b"x\n")

    def test_ambiguous_fuzzy_match_is_refused(self):
        f = self.ws / "amb.txt"
        f.write_bytes("say “hi”\nsay “hi”\n".encode("utf-8"))
        result = EditTool()(str(f), 'say "hi"', "x")
        self.assertFalse(result.success)
        self.assertIn("Found 2 matches for oldText in", result.error)
        self.assertIn("[1, 2]", result.error)
        self.assertEqual(f.read_bytes(), "say “hi”\nsay “hi”\n".encode("utf-8"))

    def test_replace_all_replaces_every_match(self):
        f = self.ws / "all.txt"
        f.write_bytes(b"foo bar foo\nfoo\n")
        result = EditTool()(str(f), "foo", "baz", replace_all=True)
        self.assertTrue(result.success, result.error)
        self.assertIn("3 occurrences", result.output)
        self.assertEqual(f.read_bytes(), b"baz bar baz\nbaz\n")

    def test_replace_all_skips_overlapping_matches(self):
        f = self.ws / "ov.txt"
        f.write_bytes(b"aaa")
        self.assertTrue(EditTool()(str(f), "aa", "b", replace_all=True).success)
        self.assertEqual(f.read_bytes(), b"ba")

    def test_not_found_tells_the_model_to_re_read(self):
        f = self.ws / "nf.txt"
        f.write_bytes(b"alpha\nbeta\n")
        result = EditTool()(str(f), "gamma", "x")
        self.assertFalse(result.success)
        self.assertIn("Could not find oldText in", result.error)
        self.assertIn("Re-read the file", result.error)

    def test_agent_passes_replace_all(self):
        agent = self.make_agent()
        (self.ws / "r.txt").write_bytes(b"x x\n")
        result = json.loads(agent._execute_tool("edit", {"path": "r.txt", "oldText": "x", "newText": "y", "replace_all": True}))
        self.assertTrue(result["success"], result["error"])
        self.assertEqual((self.ws / "r.txt").read_bytes(), b"y y\n")


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


class ReadPagingTests(TempWorkspace):
    def write_lines(self, name, count):
        f = self.ws / name
        f.write_bytes(b"".join(b"line %d\n" % i for i in range(1, count + 1)))
        return f

    def test_large_file_is_paged_with_line_numbers(self):
        f = self.write_lines("big.txt", 9000)
        self.assertGreater(f.stat().st_size, 60_000)
        first = ReadTool()(str(f))
        self.assertTrue(first.success, first.error)
        self.assertTrue(first.output.startswith("     1\tline 1\n     2\tline 2\n"))
        self.assertIn("  2000\tline 2000\n", first.output)
        self.assertTrue(first.output.endswith("[Showing lines 1-2000 of 9000. Use offset=2001 to continue.]"))

        second = ReadTool()(str(f), offset=2001)
        self.assertTrue(second.output.startswith("  2001\tline 2001\n"))
        self.assertTrue(second.output.endswith("Use offset=4001 to continue.]"))

        last = ReadTool()(str(f), offset=8001)
        self.assertTrue(last.output.endswith("  9000\tline 9000\n[End of file — 9000 lines]"))

    def test_limit_and_crlf(self):
        f = self.ws / "crlf.txt"
        f.write_bytes(b"a\r\nb\r\nc\r\nd")
        result = ReadTool()(str(f), offset=2, limit=2)
        self.assertEqual(result.output, "     2\tb\n     3\tc\n[Showing lines 2-3 of 4. Use offset=4 to continue.]")
        result = ReadTool()(str(f), offset=4)
        self.assertEqual(result.output, "     4\td\n[End of file — 4 lines]")

    def test_character_budget_ends_the_page_early(self):
        f = self.write_lines("w.txt", 100)
        result = ReadTool()(str(f), max_chars=600)
        self.assertLessEqual(len(result.output), 600)
        self.assertRegex(result.output, r"\[Showing lines 1-\d+ of 100\. Use offset=\d+ to continue\.\]$")

    def test_long_line_is_cut_with_a_marker(self):
        f = self.ws / "long.txt"
        f.write_bytes(b"x" * 5000 + b"\nshort\n")
        result = ReadTool()(str(f))
        self.assertIn("x" * 2000 + " ... [line truncated: 5,000 chars]\n", result.output)
        self.assertNotIn("x" * 2001, result.output)
        self.assertIn("     2\tshort\n", result.output)

    def test_offset_past_the_end_is_an_error(self):
        f = self.write_lines("s.txt", 3)
        result = ReadTool()(str(f), offset=10)
        self.assertFalse(result.success)
        self.assertIn("offset 10 is past the end", result.error)
        self.assertIn("(3 lines)", result.error)
        self.assertFalse(ReadTool()(str(f), offset=0).success)
        self.assertFalse(ReadTool()(str(f), limit=0).success)

    def test_empty_file(self):
        f = self.ws / "empty.txt"
        f.write_bytes(b"")
        result = ReadTool()(str(f))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, "[End of file — 0 lines]")

    def test_agent_read_page_fits_the_tool_result_cap(self):
        from kairos.agent import Agent

        f = self.ws / "wide.txt"
        f.write_bytes(b"".join(b"%05d " % i + b"y" * 100 + b"\n" for i in range(3000)))
        agent = self.make_agent()
        agent.max_tool_result_chars = 20_000
        with mock.patch.object(Agent, "TOOL_OUTPUT_DIR", self.ws / "tool-output"):
            result = json.loads(agent._execute_tool("read", {"path": "wide.txt", "offset": 5, "limit": 3000}))
        self.assertTrue(result["success"], result["error"])
        self.assertTrue(result["output"].startswith("     5\t00004 "))
        self.assertNotIn("tool output truncated", result["output"])
        self.assertLessEqual(len(result["output"]), 20_000)
        self.assertFalse((self.ws / "tool-output").exists())


class ToolOutputSaveTests(TempWorkspace):
    def make_agent(self):
        from kairos.agent import Agent

        agent = object.__new__(Agent)
        agent.max_tool_result_chars = 1_000
        patcher = mock.patch.object(Agent, "TOOL_OUTPUT_DIR", self.ws / "tool-output")
        patcher.start()
        self.addCleanup(patcher.stop)
        return agent

    def test_truncated_output_is_saved_in_full(self):
        agent = self.make_agent()
        text = "".join(f"row {i}\n" for i in range(5000))
        capped = agent._cap_tool_result({"success": True, "output": text, "error": None})
        out = capped["output"]
        self.assertLessEqual(len(out), 1_000)
        self.assertTrue(out.startswith("row 0\n"))
        self.assertTrue(out.endswith("row 4999\n"))
        self.assertIn("full output saved to ", out)
        self.assertIn("read it with offset/limit or search it", out)
        saved = list((self.ws / "tool-output").iterdir())
        self.assertEqual(len(saved), 1)
        self.assertIn(str(saved[0]), out)
        self.assertEqual(saved[0].read_bytes(), text.encode("utf-8"))

    def test_short_output_writes_nothing(self):
        agent = self.make_agent()
        capped = agent._cap_tool_result({"success": True, "output": "short", "error": None})
        self.assertEqual(capped["output"], "short")
        self.assertFalse((self.ws / "tool-output").exists())

    def test_week_old_files_are_deleted_when_writing(self):
        agent = self.make_agent()
        folder = self.ws / "tool-output"
        folder.mkdir()
        old, recent = folder / "old.txt", folder / "recent.txt"
        old.write_text("o")
        recent.write_text("r")
        week_ago = time.time() - 8 * 24 * 3600
        os.utime(old, (week_ago, week_ago))
        agent._cap_tool_result({"success": True, "output": "z" * 5_000, "error": None})
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(len(list(folder.iterdir())), 2)


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
