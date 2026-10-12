"""CLI tests: command parsing, shared event rendering, word wrap, and the
gateway/offline backends (a real gateway on a local port with a fake Agent)."""
from __future__ import annotations

import io
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich.console import Console

from kairos import main as kmain
from kairos.cli import CLI, _StreamView
from kairos.gateway_client import GatewayClient, GatewayError


def _tokens(**values):
    base = dict(session_input=10, session_output=5, context_pct=1.234, turn_input=10, turn_output=5)
    base.update(values)
    return SimpleNamespace(**base)


class StreamingFakeAgent:
    """Gateway agent_factory: streams a reply, or blocks until interrupted."""

    def __init__(self, workspace: str):
        self.workspace = workspace
        self.conversation_history = [{"role": "system", "content": "system"}]
        self.browser_manager = SimpleNamespace(is_open=False)
        self.terminal_manager = SimpleNamespace(terminals={})
        self.subagent_tool = SimpleNamespace(
            _tool_printer=None, _stream_start=None, _stream_token=None,
            _stream_end=None, _token_update=None,
        )
        self._stop = threading.Event()

    def get_history(self):
        return list(self.conversation_history)

    def run(self, content, image_url=None):
        self.conversation_history.append({"role": "user", "content": content})
        if content == "slow":
            self.on_stream_start()
            self.on_stream_token("partial ")
            self._stop.wait(10)
            self._stop.clear()
            self.on_stream_end("", False)
            return "[Interrupted]"
        self.on_tool_call("read", {"path": "C:/x.txt"})
        self.on_stream_start()
        reply = f"Hello from the [gateway] ({content}, image={bool(image_url)})"
        for word in reply.split(" "):
            self.on_stream_token(word + " ")
        self.on_stream_end(reply, False)
        self.on_token_update(_tokens())
        if self.subagent_tool._token_update:
            self.subagent_tool._token_update("sub1", _tokens(turn_output=7))
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def interrupt(self):
        self._stop.set()

    def compact(self):
        return "nothing to compact"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GatewayServer:
    """Run the real FastAPI gateway with uvicorn on a spare local port."""

    def __init__(self, data_dir: Path, default_workspace: str | None = None):
        import uvicorn
        from kairos.gateway.manager import GatewayManager
        from kairos.gateway.repository import GatewayRepository
        from kairos.gateway.server import create_app

        self.manager = GatewayManager(
            GatewayRepository(data_dir),
            default_workspace=default_workspace,
            max_concurrent_runs=2,
            agent_factory=StreamingFakeAgent,
        )
        port = _free_port()
        self.url = f"ws://127.0.0.1:{port}/api/v1/ws"
        self.server = uvicorn.Server(uvicorn.Config(
            create_app(self.manager), host="127.0.0.1", port=port, log_level="warning",
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("gateway did not start")
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(15)


class ParseCommandTests(unittest.TestCase):
    def test_commands_and_messages(self):
        self.assertEqual(kmain.parse_command("/quit"), ("/quit", []))
        self.assertEqual(kmain.parse_command("  /Resume 2 "), ("/resume", ["2"]))
        self.assertEqual(kmain.parse_command("/model gpt-5 openai"), ("/model", ["gpt-5", "openai"]))
        self.assertEqual(kmain.parse_command("/paste"), ("/paste", []))  # reported as unknown
        self.assertIsNone(kmain.parse_command("exit"))
        self.assertIsNone(kmain.parse_command("clear the cache"))
        self.assertIsNone(kmain.parse_command("/etc/hosts has a typo"))
        self.assertIsNone(kmain.parse_command("/"))
        self.assertIsNone(kmain.parse_command(""))

    def test_unknown_and_removed_commands_are_not_sent_to_the_agent(self):
        cli, out = _recording_cli()
        backend = SimpleNamespace()
        for name in ("/exit", "/paste", "/help"):
            self.assertFalse(kmain.handle_command(cli, backend, name, []))
        self.assertIn("Unknown command /paste", out.getvalue())

    def test_args_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            args = kmain.parse_args([directory, "--offline"])
            self.assertTrue(args.offline)
            self.assertEqual(args.workspace, str(Path(directory).resolve()))
        self.assertIsNone(kmain.parse_args([]).workspace)
        with self.assertRaises(SystemExit):
            kmain.parse_args([str(ROOT / "does-not-exist")])


def _recording_cli(width: int = 60, height: int = 12) -> tuple[CLI, io.StringIO]:
    # PromptSession needs a real console; rendering tests do not prompt.
    with mock.patch("kairos.cli.PromptSession"):
        cli = CLI()
    out = io.StringIO()
    cli.console = Console(file=out, width=width, height=height, color_system=None, force_terminal=False)
    return cli, out


LONG_TEXT = (
    "Supercalifragilistic words should never be split across lines when the "
    "terminal wraps a long answer; every word must stay whole, including "
    "internationalization and [bracketed] text that looks like markup. "
) * 4


class RenderingTests(unittest.TestCase):
    def test_events_render_like_the_offline_callbacks(self):
        cli, out = _recording_cli(width=100)
        cli.handle_event("tool.started", {"name": "read", "summary": "read [/bold] C:/x"})
        cli.handle_event("assistant.stream_started", {})
        for word in "Final **answer** here".split(" "):
            cli.handle_event("assistant.token", {"delta": word + " "})
        cli.handle_event("assistant.stream_ended", {"content": "Final **answer** here", "has_tool_calls": False})
        cli.handle_event("token.updated", {"session_input": 1200, "session_output": 5, "context_pct": 1.234, "turn_input": 1200, "turn_output": 5})
        cli.handle_event("token.updated", {"session_input": 1, "session_output": 2, "context_pct": 0, "turn_input": 1, "turn_output": 2, "subagent_id": "sub1"})
        cli.handle_event("conversation.compacted", {"message": "Compacted [ok]"})
        cli.handle_event("run.queued", {})  # ignored
        text = out.getvalue()
        self.assertIn("read [/bold] C:/x", text)  # markup-like text printed literally
        self.assertIn("Final answer here", text)
        self.assertTrue(cli._skip_print_response)
        self.assertIn("Session: 1,200 in / 5 out  |  Context: 1.2%  |  Turn: 1,200 in / 5 out", text)
        self.assertIn("Subagent sub1: Session: 1 in / 2 out", text)
        self.assertIn("Compacted [ok]", text)

    def test_tool_preamble_stays_as_grey_trace(self):
        cli, out = _recording_cli()
        cli.handle_event("assistant.stream_started", {})
        cli.handle_event("assistant.token", {"delta": "Let me look [x]."})
        cli.handle_event("assistant.stream_ended", {"content": "Let me look [x].", "has_tool_calls": True})
        self.assertIn("Let me look [x].", out.getvalue())
        self.assertFalse(cli._skip_print_response)

    def test_long_output_wraps_at_word_boundaries(self):
        width = 50
        cli, out = _recording_cli(width=width)
        # The streaming panel's final frame and the final markdown panel.
        view = _StreamView(SimpleNamespace(_stream_text=LONG_TEXT))
        view.full = True
        cli.console.print(view)
        cli.print_response(LONG_TEXT)
        lines = out.getvalue().splitlines()
        self.assertTrue(all(len(line) <= width for line in lines))
        rendered_words = set()
        for line in lines:
            rendered_words.update(line.strip("│╭╮╰╯─ ").split())
        for word in LONG_TEXT.split():
            self.assertIn(word, rendered_words, f"word was split: {word}")

    def test_stream_view_shows_the_latest_lines_while_streaming(self):
        console = Console(file=io.StringIO(), width=40, height=10, color_system=None)
        cli = SimpleNamespace(_stream_text=" ".join(f"word{i}" for i in range(400)))
        view = _StreamView(cli)
        lines = console.render_lines(view, console.options, pad=False)
        self.assertLessEqual(len(lines), 9)
        self.assertIn("word399", "".join(seg.text for seg in lines[-2]))
        view.full = True
        self.assertGreater(len(console.render_lines(view, console.options, pad=False)), 9)

    def test_banner_is_one_line(self):
        cli, out = _recording_cli(width=80)
        cli.print_banner("C:/work", "gateway")
        self.assertEqual(out.getvalue().strip().splitlines(), ["KAIROS  gateway  C:/work"])


class GatewayBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.workspace = root / "project"
        cls.workspace.mkdir()
        # The gateway gives workspace-less conversations a fresh folder from
        # kairos.workspaces; point it at the temp dir so tests never touch ~/Documents.
        from kairos import workspaces
        cls._workspace_root = workspaces.WORKSPACE_ROOT
        cls.scratch_root = root / "kairos_workspace"
        workspaces.WORKSPACE_ROOT = cls.scratch_root
        cls.gateway = GatewayServer(root / "data")

    @classmethod
    def tearDownClass(cls):
        from kairos import workspaces
        workspaces.WORKSPACE_ROOT = cls._workspace_root
        cls.gateway.stop()
        cls.tmp.cleanup()

    def backend(self, workspace: str | None) -> kmain.GatewayBackend:
        backend = kmain.GatewayBackend(workspace, GatewayClient(self.gateway.url, token=""))
        self.addCleanup(backend.close)
        return backend

    def test_streams_a_reply_and_resumes(self):
        backend = self.backend(str(self.workspace))
        self.assertEqual(Path(backend.workspace), self.workspace.resolve())
        events = []
        outcome = backend.run("hi", "data:image/png;base64,AAAA", threading.Event(), lambda e, d: events.append((e, d)))
        self.assertEqual(outcome, ("completed", "Hello from the [gateway] (hi, image=True)"))
        names = [e for e, _ in events]
        self.assertEqual(names[names.index("tool.started") + 1], "assistant.stream_started")
        self.assertGreater(names.count("assistant.token"), 3)
        self.assertIn("assistant.stream_ended", names)
        token_updates = [d for e, d in events if e == "token.updated"]
        self.assertEqual(token_updates[-1]["subagent_id"], "sub1")
        streamed = "".join(d["delta"] for e, d in events if e == "assistant.token")
        self.assertEqual(streamed.strip(), "Hello from the [gateway] (hi, image=True)")

        # Token deltas are live-only; the full text is persisted once.
        stored = [e["event"] for e in self.gateway.manager.events_after(backend.conversation["id"])]
        self.assertNotIn("assistant.token", stored)
        self.assertIn("assistant.stream_ended", stored)

        self.assertEqual(backend.compact(), "nothing to compact")
        listed = backend.list_conversations()
        self.assertIn(backend.conversation["id"], [c["id"] for c in listed])
        self.assertTrue(all(c["preview"] for c in listed))

        other = self.backend(str(self.workspace))
        last, needs_continue = other.resume(backend.conversation["id"])
        self.assertEqual(last, "Hello from the [gateway] (hi, image=True)")
        self.assertFalse(needs_continue)

    def test_escape_returns_at_once_and_next_message_still_runs(self):
        backend = self.backend(str(self.workspace))
        stop = threading.Event()
        events = []

        def on_event(name, data):
            events.append(name)
            if name == "assistant.token":
                stop.set()  # what the Esc listener does

        started = time.monotonic()
        self.assertEqual(backend.run("slow", None, stop, on_event), ("interrupted", None))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(backend._abandoned), 1)
        # The next message queues behind the stopped run and completes.
        outcome = backend.run("again", None, threading.Event(), lambda *_: None)
        self.assertEqual(outcome[0], "completed")
        backend._settle(5)
        self.assertFalse(backend._abandoned)

    def test_new_conversation_without_path_and_listing_by_folder(self):
        backend = self.backend(None)
        self.assertEqual(Path(backend.workspace).parent, self.scratch_root.resolve())
        backend.run("scratch question", None, threading.Event(), lambda *_: None)
        explicit = self.backend(str(self.workspace))
        explicit.run("project question", None, threading.Event(), lambda *_: None)
        scratch_ids = {c["id"] for c in backend.list_conversations()}
        project_ids = {c["id"] for c in explicit.list_conversations()}
        self.assertIn(backend.conversation["id"], scratch_ids)
        self.assertNotIn(explicit.conversation["id"], scratch_ids)
        self.assertNotIn(backend.conversation["id"], project_ids)

    def test_load_reports_unfinished_history(self):
        conversation = self.gateway.manager.create_conversation(str(self.workspace))
        self.gateway.manager.repository.replace_messages(conversation.id, [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "unanswered"},
        ])
        backend = self.backend(str(self.workspace))
        last, needs_continue = backend.resume(conversation.id)
        self.assertEqual((last, needs_continue), ("done", True))
        outcome = backend.continue_run(threading.Event(), lambda *_: None)
        self.assertEqual(outcome[0], "completed")

    def test_errors_and_disconnect(self):
        client = GatewayClient(self.gateway.url, token="")
        client.connect()
        with self.assertRaises(GatewayError) as caught:
            client.request("conversation.load", "conv_missing")
        self.assertEqual(caught.exception.code, "not_found")
        client.close()
        with self.assertRaises(GatewayError):
            for _ in range(40):
                client.next_event(0.05)
        self.assertTrue(client.closed)
        with self.assertRaises(GatewayError) as caught:
            GatewayClient("ws://127.0.0.1:1/api/v1/ws", token="").connect()
        self.assertEqual(caught.exception.code, "disconnected")


class FakeClient:
    """Records gateway requests for the model commands (subagent A's API)."""

    def __init__(self):
        self.requests = []
        self.info = {"setup": "fast", "provider": "openai", "model": "gpt-5-mini", "format": "openai",
                     "effort": "low", "reasoning_efforts": ["low", "medium", "high"]}
        self.closed = False

    def connect(self):
        return {}

    def close(self):
        pass

    def next_event(self, timeout):
        return None

    def request(self, command, conversation_id=None, payload=None, timeout=None):
        self.requests.append((command, conversation_id, dict(payload or {})))
        if command == "conversation.create":
            return {"conversation": {"id": f"conv{len(self.requests)}", "workspace_path": "C:/w", "last_event_id": 0}}
        if command == "model.list":
            return {"setups": [
                {"name": "fast", "provider": "openai", "model": "gpt-5-mini"},
                {"name": "smart", "provider": "anthropic", "model": "claude-opus"},
            ], "default_setup": "fast"}
        if command == "conversation.configure":
            payload = payload or {}
            if "setup" in payload:
                self.info.update(setup=payload["setup"], effort="medium")
            if "model" in payload:
                self.info.update(setup=None, model=payload["model"], provider=payload["provider"])
            if "effort" in payload:
                self.info["effort"] = payload["effort"]
            return dict(self.info)
        return {}


class ModelCommandTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.backend = kmain.GatewayBackend(None, self.client)
        self.cli, self.out = _recording_cli(width=100)

    def configures(self):
        # Requests that change something (model_info sends only the id).
        return [p for c, _, p in self.client.requests if c == "conversation.configure" and len(p) > 1]

    def test_status_and_pick_by_number_or_name(self):
        self.assertEqual(kmain._status_text(self.backend), "gpt-5-mini · low")
        kmain.handle_command(self.cli, self.backend, "/model", ["2"])
        kmain.handle_command(self.cli, self.backend, "/model", ["fast"])
        self.assertEqual([p.get("setup") for p in self.configures() if "setup" in p], ["smart", "fast"])
        self.assertIn("Model: fast (openai/gpt-5-mini)", self.out.getvalue())

    def test_model_list_then_pick(self):
        self.cli.ask = lambda label: "2"
        kmain.handle_command(self.cli, self.backend, "/model", [])
        text = self.out.getvalue()
        self.assertIn("1. fast", text)
        self.assertIn("(current)", text)
        self.assertEqual(self.configures()[-1]["setup"], "smart")

    def test_adhoc_model_effort_and_reset_keeps_choice(self):
        kmain.handle_command(self.cli, self.backend, "/model", ["gpt-5", "OpenRouter"])
        self.assertEqual(self.configures()[-1], {"conversation_id": "conv1", "model": "gpt-5", "provider": "openrouter"})
        kmain.handle_command(self.cli, self.backend, "/effort", [])
        self.assertIn("allowed: low, medium, high", self.out.getvalue())
        kmain.handle_command(self.cli, self.backend, "/effort", ["HIGH"])
        self.assertEqual(self.configures()[-1]["effort"], "high")
        kmain.handle_command(self.cli, self.backend, "/reset", [])
        self.assertEqual(
            self.configures()[-1],
            {"conversation_id": self.backend.conversation["id"], "model": "gpt-5", "provider": "openrouter", "effort": "high"},
        )
        self.assertNotEqual(self.backend.conversation["id"], "conv1")

    def test_bad_model_arguments(self):
        kmain.handle_command(self.cli, self.backend, "/model", ["gpt-5", "azure"])
        kmain.handle_command(self.cli, self.backend, "/model", ["nope"])
        self.assertEqual(self.configures(), [])
        self.assertIn("Usage: /model", self.out.getvalue())

    def test_status_is_empty_when_gateway_lacks_configure(self):
        def fail(*args, **kwargs):
            raise GatewayError("Unknown command: conversation.configure", "unknown_command")

        self.client.request = fail
        self.assertEqual(kmain._status_text(self.backend), "")


class FakeOfflineAgent:
    instances = []

    def __init__(self, workspace):
        type(self).instances.append(self)
        self.cwd = Path(workspace)
        self.conversation_history = [{"role": "system", "content": "system"}]
        self.terminal_manager = SimpleNamespace(terminals={})
        self.browser_manager = SimpleNamespace(is_open=False)
        self.subagent_tool = SimpleNamespace()
        self.configured = []
        self._stop = threading.Event()

    @staticmethod
    def _tool_summary(name, args):
        return f"{name} {args}"

    def get_history(self):
        return list(self.conversation_history)

    def run(self, text, image_url=None):
        self.on_tool_call("read", {"path": "a"})
        self.on_stream_start()
        if text == "slow":
            self.on_stream_token("partial")
            self._stop.wait(10)
            self.on_stream_end("", False)
            return "[Interrupted]"
        self.on_stream_token("done")
        self.on_stream_end("done", False)
        self.on_token_update(_tokens())
        self.conversation_history += [{"role": "user", "content": text}, {"role": "assistant", "content": "done"}]
        return "done"

    def interrupt(self):
        self._stop.set()

    def compact(self):
        return "compacted"

    def reset(self):
        self.conversation_history = self.conversation_history[:1]

    def set_model(self, setup=None, model=None, provider=None):
        self.configured.append(("model", setup, model, provider))

    def set_effort(self, effort):
        self.configured.append(("effort", effort))

    def model_info(self):
        return {"setup": "fast", "provider": "openai", "model": "m", "effort": "low", "reasoning_efforts": ["low"]}


class OfflineBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.created = []

        def new_workspace():
            path = root / "kairos_workspace" / f"chat-{len(self.created) + 1}"
            path.mkdir(parents=True)
            self.created.append(path)
            return path

        # kairos.workspaces (subagent C) and the real Agent are stubbed.
        stubs = {
            "kairos.workspaces": types.SimpleNamespace(new_workspace=new_workspace),
            "kairos.agent": types.SimpleNamespace(Agent=FakeOfflineAgent),
        }
        saved = {name: sys.modules.get(name) for name in stubs}
        sys.modules.update(stubs)
        self.addCleanup(lambda: [sys.modules.pop(n, None) if m is None else sys.modules.__setitem__(n, m) for n, m in saved.items()])

        home = root / "home"
        self._home = os.environ.get("USERPROFILE"), os.environ.get("HOME")
        os.environ["USERPROFILE"] = os.environ["HOME"] = str(home)
        self.addCleanup(self._restore_home)
        FakeOfflineAgent.instances = []

    def _restore_home(self):
        for key, value in zip(("USERPROFILE", "HOME"), self._home):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_run_escape_reset_and_resume(self):
        backend = kmain.OfflineBackend(None)
        self.assertEqual(Path(backend.workspace), self.created[0])
        events = []
        self.assertEqual(backend.run("hi", None, threading.Event(), lambda e, d: events.append(e)), ("completed", "done"))
        self.assertEqual(events[:3], ["tool.started", "assistant.stream_started", "assistant.token"])
        self.assertIn("token.updated", events)

        stop = threading.Event()
        started = time.monotonic()
        outcome = backend.run("slow", None, stop, lambda e, d: stop.set() if e == "assistant.token" else None)
        self.assertEqual(outcome, ("interrupted", None))
        self.assertLess(time.monotonic() - started, 2.0)

        backend.configure(setup="smart")
        backend.configure(effort="high")
        backend.reset()  # no path: a new folder and Agent with the same model
        self.assertEqual(Path(backend.workspace), self.created[1])
        self.assertEqual(FakeOfflineAgent.instances[-1].configured, [("model", "smart", None, None), ("effort", "high")])

        sessions = backend.list_conversations()
        self.assertEqual(len(sessions), 1)  # the first chat, saved under the shared folder
        last, mid_execution = backend.resume(sessions[0]["id"])
        self.assertEqual(last, "done")
        backend.close()


if __name__ == "__main__":
    unittest.main()
