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
        cli.handle_event("token.updated", {"session_input": 1200, "session_output": 5, "context_pct": 1.234, "turn_input": 1200, "turn_output": 5,
                                             "context_tokens": 3702, "context_window": 300000, "session_cost": 0.0123})
        cli.handle_event("conversation.compacted", {"message": "Compacted [ok]"})
        cli.handle_event("run.queued", {})  # ignored
        text = out.getvalue()
        self.assertIn("read [/bold] C:/x", text)  # markup-like text printed literally
        self.assertIn("Final answer here", text)
        self.assertTrue(cli._skip_print_response)
        self.assertIn("Context 1.2% · 3,702 / 300,000 tokens · this call: 5 out · session: 1,200 in / 5 out · $0.0123", text)
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
        # Sub-agent usage now arrives in subagent.status, not token.updated.
        self.assertNotIn("subagent_id", token_updates[-1])
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
        if command == "subagent.stop_all":
            return {"stopped": 3}
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
        self.received = []
        self.background = []
        self.stopped_subagents = 0
        self.interrupts = 0
        self._stop = threading.Event()

    @staticmethod
    def _tool_summary(name, args):
        return f"{name} {args}"

    def get_history(self):
        return list(self.conversation_history)

    def stop_subagents(self):
        self.stopped_subagents += 1
        return 2

    def subagent_statuses(self):
        return []

    def add_background_events(self, events):
        self.background.extend(events)

    def run(self, text, image_url=None):
        self.received.append(text)
        self.on_tool_call("read", {"path": "a"})
        self.on_stream_start()
        if text.endswith("slow"):  # also a wake turn for a "slow" notification
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
        self.interrupts += 1
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


class FakeScheduler:
    """kairos.wake.WakeScheduler stand-in (subagent D's contract)."""

    def __init__(self, start_wake, is_idle, debounce=2.0, cap=10, on_paused=None):
        self.start_wake, self.is_idle, self.on_paused = start_wake, is_idle, on_paused
        self.pushed, self.pending, self.user_messages = [], [], 0

    def push(self, event):
        self.pushed.append(event)

    def take_pending(self):
        pending, self.pending = self.pending, []
        return pending

    def user_message_sent(self):
        self.user_messages += 1


class OfflineCase(unittest.TestCase):
    """Stubs the Agent, kairos.workspaces and kairos.wake for OfflineBackend."""

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
            "kairos.wake": types.SimpleNamespace(WakeScheduler=FakeScheduler),
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


class OfflineBackendTests(OfflineCase):
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


# ---------------------------------------------------------------------- #
#  Background work: wake turns, sub-agent status, /stop                    #
# ---------------------------------------------------------------------- #

SUBAGENT_DONE = {"id": "n1", "kind": "subagent_finished", "subagent_id": "a3f2", "text": "sub-agent a3f2 result: 42", "created_at": "t"}
TERMINAL_HIT = {"id": "n2", "kind": "terminal_matched", "terminal_id": 2, "text": "terminal 2 printed READY", "created_at": "t"}


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


class BackgroundRenderingTests(unittest.TestCase):
    def test_wake_turn_header_reply_and_end(self):
        cli, out = _recording_cli(width=100)
        cli.handle_event("wake.started", {"notifications": [SUBAGENT_DONE, TERMINAL_HIT]})
        self.assertTrue(cli._wake)
        cli.handle_event("tool.started", {"name": "read", "summary": "read C:/result.txt"})
        cli.handle_event("assistant.stream_started", {})
        cli.handle_event("assistant.token", {"delta": "The sub-agent found 42."})
        cli.handle_event("assistant.stream_ended", {"content": "The sub-agent found 42.", "has_tool_calls": False})
        cli.handle_event("wake.ended", {"outcome": "completed", "payload": "The sub-agent found 42."})
        text = out.getvalue()
        self.assertIn("» woke: sub-agent a3f2 finished, terminal 2 matched", text)
        self.assertIn("→ read C:/result.txt", text)
        self.assertEqual(text.count("The sub-agent found 42."), 1)  # not printed twice
        self.assertFalse(cli._wake)
        self.assertFalse(cli._skip_print_response)

    def test_stopped_wake_and_generic_header(self):
        cli, out = _recording_cli(width=100)
        cli.handle_event("wake.started", {"notifications": []})
        cli.handle_event("assistant.stream_started", {})
        cli.handle_event("assistant.token", {"delta": "half an answer"})
        cli.handle_event("wake.ended", {"outcome": "interrupted"})
        cli.handle_event("wake.ended", {"outcome": "interrupted"})  # ignored: already ended
        text = out.getvalue()
        self.assertIn("» woke: background work finished", text)
        self.assertIn("half an answer", text)  # the stopped reply stays as a grey trace
        self.assertEqual(text.count("Interrupted."), 1)
        self.assertIsNone(cli._live)

    def test_paused_line(self):
        cli, out = _recording_cli(width=100)
        cli.handle_event("wake.paused", {"conversation_id": "c", "pending": 3})
        cli.handle_event("wake.paused", {"conversation_id": "c", "pending": 1})
        lines = out.getvalue().splitlines()
        self.assertEqual(lines, [
            "paused auto-wake: 3 background results will be sent with your next message",
            "paused auto-wake: 1 background result will be sent with your next message",
        ])

    def test_subagent_status_lines_in_the_toolbar(self):
        cli, out = _recording_cli()
        session = cli._prompt_session
        status = {"conversation_id": "c", "subagent_id": "a3f2", "state": "running", "current_tool": "read",
                  "elapsed_s": 65, "prompt": "find it"}
        cli.handle_event("subagent.status", status)
        cli.handle_event("subagent.status", {**status, "subagent_id": "b7c1", "current_tool": None, "elapsed_s": 3})
        self.assertEqual(session.bottom_toolbar, cli._toolbar)
        lines = cli._toolbar()[0][1].splitlines()
        self.assertEqual(lines, ["●  a3f2  running  read  1m05s", "●  b7c1  running  3s"])
        # Elapsed time keeps counting between status events.
        cli._subagents["b7c1"]["_seen"] -= 10
        self.assertEqual(cli._toolbar()[0][1].splitlines()[1], "●  b7c1  running  13s")
        cli.handle_event("subagent.status", {**status, "state": "idle"})
        self.assertEqual(list(cli._subagents), ["b7c1"])
        cli.handle_event("subagent.status", {**status, "subagent_id": "b7c1", "state": "terminated"})
        self.assertIsNone(session.bottom_toolbar)
        cli.handle_event("subagent.list", {"subagents": [status]})
        self.assertEqual(list(cli._subagents), ["a3f2"])
        cli.handle_event("subagent.list", {"subagents": []})
        self.assertIsNone(session.bottom_toolbar)
        self.assertEqual(out.getvalue(), "")  # no sub-agent text is printed


class StopCommandTests(OfflineCase):
    def test_stop_through_the_gateway(self):
        client = FakeClient()
        backend = kmain.GatewayBackend(None, client)
        cli, out = _recording_cli(width=100)
        self.assertFalse(kmain.handle_command(cli, backend, "/stop", []))
        self.assertIn(("subagent.stop_all", "conv1", {"conversation_id": "conv1"}), client.requests)
        self.assertIn("Stopped 3 sub-agents.", out.getvalue())
        self.assertIn("/stop", kmain.COMMANDS)

    def test_stop_offline(self):
        backend = kmain.OfflineBackend(None)
        cli, out = _recording_cli(width=100)
        kmain.handle_command(cli, backend, "/stop", [])
        self.assertEqual(backend.agent.stopped_subagents, 1)
        self.assertIn("Stopped 2 sub-agents.", out.getvalue())


class OfflineWakeTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.backend = kmain.OfflineBackend(None)
        self.addCleanup(self.backend.close)
        self.events = []
        self.backend.start_background(lambda name, data: self.events.append((name, data)))
        self.scheduler = self.backend._scheduler
        self.agent = self.backend.agent

    def names(self):
        return [name for name, _ in self.events]

    def test_scheduler_wiring(self):
        self.assertIsInstance(self.scheduler, FakeScheduler)
        self.agent.on_background_event(SUBAGENT_DONE)
        self.assertEqual(self.scheduler.pushed, [SUBAGENT_DONE])
        self.assertTrue(self.scheduler.is_idle())
        self.agent.on_subagent_status({"subagent_id": "a3f2", "state": "running"})
        self.scheduler.on_paused(2)
        self.assertEqual(self.events, [
            ("subagent.status", {"subagent_id": "a3f2", "state": "running"}),
            ("wake.paused", {"pending": 2}),
        ])

    def test_wake_turn_renders_like_a_run(self):
        self.scheduler.start_wake([SUBAGENT_DONE])
        _wait_for(lambda: "wake.ended" in self.names())
        self.assertEqual(self.events[0], ("wake.started", {"notifications": [SUBAGENT_DONE]}))
        self.assertEqual(self.names()[1:4], ["tool.started", "assistant.stream_started", "assistant.token"])
        self.assertEqual(self.events[-1], ("wake.ended", {"outcome": "completed", "payload": "done"}))
        message = self.agent.received[-1]
        self.assertEqual(message, kmain.format_notifications([SUBAGENT_DONE]))
        self.assertIn("sub-agent a3f2 result: 42", message)
        # /resume does not mistake the wake message for something the user typed.
        from kairos.resume import _is_synthetic_user_message
        self.assertTrue(_is_synthetic_user_message({"role": "user", "content": message}))

    def test_one_run_at_a_time_and_pending_results_join_the_next_message(self):
        stop = threading.Event()
        started = threading.Event()
        idle_during_run = []

        def on_event(name, data):
            if name == "assistant.token":
                idle_during_run.append(self.scheduler.is_idle())
                started.set()

        self.scheduler.pending = [TERMINAL_HIT]
        outcome = self.backend.run("hi", None, stop, on_event)
        self.assertEqual(outcome, ("completed", "done"))
        self.assertEqual(idle_during_run, [False])
        self.assertEqual(self.scheduler.user_messages, 1)
        self.assertEqual(self.agent.received[-1], "hi")
        self.assertEqual(self.agent.background, [TERMINAL_HIT])  # injected by the agent, as in the gateway
        _wait_for(self.scheduler.is_idle)

        # A wake that fires while a user turn runs waits for it.
        thread = threading.Thread(target=self.backend.run, args=("slow", None, threading.Event(), lambda *_: None))
        thread.start()
        _wait_for(lambda: not self.scheduler.is_idle())
        self.scheduler.start_wake([SUBAGENT_DONE])
        time.sleep(0.2)
        self.assertNotIn("wake.started", self.names())
        self.agent.interrupt()  # end the slow turn
        thread.join(5)
        _wait_for(lambda: "wake.ended" in self.names())

    def test_esc_stops_the_wake_turn_once_and_the_agent_is_free_again(self):
        self.agent._stop.clear()
        self.scheduler.start_wake([{"kind": "subagent_finished", "subagent_id": "x", "text": "slow"}])
        _wait_for(lambda: "assistant.token" in self.names())
        self.assertTrue(self.backend.stop_wake())
        self.assertFalse(self.backend.stop_wake())
        self.assertEqual(self.events[-1], ("wake.ended", {"outcome": "interrupted", "payload": None}))
        self.assertEqual(self.agent.interrupts, 1)
        _wait_for(self.scheduler.is_idle)
        self.assertEqual(self.names().count("wake.ended"), 1)
        self.agent._stop.clear()
        self.assertEqual(self.backend.run("hi", None, threading.Event(), lambda *_: None)[0], "completed")

    def test_esc_while_a_wake_turn_holds_the_agent_drops_the_message(self):
        self.agent._stop.clear()
        self.scheduler.start_wake([{"kind": "subagent_finished", "subagent_id": "x", "text": "slow"}])
        _wait_for(lambda: "assistant.token" in self.names())
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        self.assertEqual(self.backend.run("hi", None, stop, lambda *_: None), ("interrupted", None))
        self.assertNotIn("hi", self.agent.received)
        self.assertEqual(self.agent.interrupts, 0)  # the wake turn keeps going
        self.backend.stop_wake()


class GatewayWakeTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.backend = kmain.GatewayBackend(None, self.client)
        self.events = []
        self.backend._view = lambda name, data: self.events.append((name, data))

    def feed(self, name, data=None, run_id=None, conversation_id="conv1"):
        self.backend._background({"event": name, "data": data or {}, "run_id": run_id, "conversation_id": conversation_id})

    def test_wake_run_is_rendered_with_a_header_from_the_notifications(self):
        self.feed("background.notification", {"notifications": [SUBAGENT_DONE]})
        self.feed("background.notification", {"notifications": [TERMINAL_HIT]})
        self.feed("run.queued", {"origin": "wake"}, "run_w")
        self.feed("run.started", {"origin": "wake", "run": {"id": "run_w"}}, "run_w")
        self.feed("assistant.token", {"delta": "hi"}, "run_w")
        self.feed("assistant.token", {"delta": "other client"}, "run_other")
        self.feed("subagent.status", {"subagent_id": "a3f2", "state": "idle"})
        self.feed("wake.paused", {"pending": 1})
        self.feed("subagent.status", {"subagent_id": "zz", "state": "running"}, conversation_id="old_conv")
        self.feed("run.completed", {"response": "ok"}, "run_w")
        self.assertEqual(self.events, [
            ("wake.started", {"notifications": [SUBAGENT_DONE, TERMINAL_HIT]}),
            ("assistant.token", {"delta": "hi"}),
            ("subagent.status", {"subagent_id": "a3f2", "state": "idle"}),
            ("wake.paused", {"pending": 1}),
            ("wake.ended", {"outcome": "completed", "payload": "ok"}),
        ])
        self.assertEqual(self.backend._notes, [])

    def test_user_runs_are_not_wake_runs_and_sending_clears_notes(self):
        self.feed("background.notification", {"notifications": [SUBAGENT_DONE]})
        self.feed("run.started", {"origin": "user"}, "run_u")
        self.feed("assistant.token", {"delta": "x"}, "run_u")
        self.assertEqual(self.events, [])
        self.client.request = lambda *a, **k: {"run_id": "run_mine"}
        self.client.next_event = lambda timeout: {"event": "run.completed", "run_id": "run_mine", "data": {"response": "r"}}
        self.assertEqual(self.backend.run("hi", None, threading.Event(), lambda *_: None), ("completed", "r"))
        self.assertEqual(self.backend._notes, [])

    def test_esc_interrupts_the_wake_run_and_drops_its_output(self):
        self.feed("run.started", {"origin": "wake"}, "run_w")
        self.assertTrue(self.backend.stop_wake())
        self.assertFalse(self.backend.stop_wake())
        self.assertEqual(self.events[-1], ("wake.ended", {"outcome": "interrupted", "payload": None}))
        self.feed("assistant.token", {"delta": "late"}, "run_w")
        self.feed("run.interrupted", {}, "run_w")
        self.assertEqual([name for name, _ in self.events], ["wake.started", "wake.ended"])
        _wait_for(lambda: ("run.interrupt", "conv1", {"run_id": "run_w"}) in self.client.requests)
        self.assertFalse(self.backend._abandoned)  # its end event arrived

    def test_wake_output_while_waiting_for_an_own_queued_run(self):
        queue_ = [
            {"event": "run.started", "run_id": "run_w", "conversation_id": "conv1", "data": {"origin": "wake"}},
            {"event": "assistant.token", "run_id": "run_w", "conversation_id": "conv1", "data": {"delta": "w"}},
            {"event": "run.completed", "run_id": "run_w", "conversation_id": "conv1", "data": {"response": "w"}},
            {"event": "assistant.token", "run_id": "run_mine", "conversation_id": "conv1", "data": {"delta": "m"}},
            {"event": "run.completed", "run_id": "run_mine", "conversation_id": "conv1", "data": {"response": "m"}},
        ]
        self.client.request = lambda *a, **k: {"run_id": "run_mine"}
        self.client.next_event = lambda timeout: queue_.pop(0) if queue_ else None
        own = []
        outcome = self.backend.run("hi", None, threading.Event(), lambda name, data: own.append(name))
        self.assertEqual(outcome, ("completed", "m"))
        self.assertEqual(own, ["assistant.token"])
        self.assertEqual([name for name, _ in self.events], ["wake.started", "assistant.token", "wake.ended"])

    def test_subagents_are_listed_when_background_starts_and_on_reset(self):
        self.client.next_event = lambda timeout: time.sleep(timeout)
        self.backend._view = None
        self.backend.start_background(lambda name, data: self.events.append((name, data)))
        self.addCleanup(setattr, self.client, "closed", True)
        self.assertEqual(self.events, [("subagent.list", {"subagents": []})])
        self.backend.reset()
        self.assertEqual(self.events[-1], ("subagent.list", {"subagents": []}))
        self.assertEqual(sum(1 for c, _, _ in self.client.requests if c == "subagent.list"), 2)


class PromptCoexistenceTests(unittest.TestCase):
    """Background output prints above an open prompt; typed text survives."""

    def test_wake_output_above_the_prompt_keeps_the_typed_text(self):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output

        class Screen(Vt100_Output):
            # A real console knows the rows below the cursor; the toolbar
            # is drawn only then.
            def get_rows_below_cursor_position(self):
                return 20

        screen = io.StringIO()
        with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=Screen(screen, lambda: Size(rows=40, columns=90), enable_cpr=False)
        ):
            cli = CLI()
            cli.console = Console(file=io.StringIO(), width=90, color_system=None)
            escapes = []
            cli.on_prompt_escape = lambda: escapes.append(True) or cli.handle_event(
                "wake.ended", {"outcome": "interrupted"})
            seen = {}
            buffer = cli._prompt_session.default_buffer
            app = cli._prompt_session.app

            def background():
                try:
                    _wait_for(lambda: app.is_running)
                    pipe.send_text("hello wor")
                    _wait_for(lambda: buffer.text == "hello wor")
                    cli.handle_event("subagent.status", {"subagent_id": "a3f2", "state": "running",
                                                         "current_tool": "search", "elapsed_s": 5})
                    cli.handle_event("wake.started", {"notifications": [SUBAGENT_DONE]})
                    cli.handle_event("tool.started", {"name": "read", "summary": "read C:/notes.txt"})
                    cli.handle_event("assistant.stream_started", {})
                    for word in "Wake answer: the sub-agent found 42".split():
                        cli.handle_event("assistant.token", {"delta": word + " "})
                    _wait_for(lambda: "Wake answer" in "".join(t for _, t, *_ in cli._render_preview()))
                    cli.handle_event("assistant.stream_ended", {"content": "Wake answer: **the sub-agent found 42**",
                                                                "has_tool_calls": False})
                    cli.handle_event("wake.ended", {"outcome": "completed", "payload": "x"})
                    _wait_for(lambda: "the sub-agent found 42" in screen.getvalue())
                    seen["after_output"] = (buffer.text, buffer.cursor_position)
                    # Esc stops a running wake turn (and only then).
                    cli.handle_event("wake.started", {"notifications": [TERMINAL_HIT]})
                    pipe.send_text("\x1b")
                    _wait_for(lambda: escapes)
                    _wait_for(lambda: "Interrupted." in screen.getvalue())
                    seen["after_escape"] = buffer.text
                    pipe.send_text("ld\r")
                except Exception as exc:  # surface failures in the main thread
                    seen["error"] = exc
                    app.exit(result="")

            helper = threading.Thread(target=background, daemon=True)
            helper.start()
            result = cli.get_user_input()
            helper.join(5)

        self.assertNotIn("error", seen, seen.get("error"))
        self.assertEqual(seen["after_output"], ("hello wor", 9))
        self.assertEqual(seen["after_escape"], "hello wor")
        self.assertEqual(result, "hello world")
        text = screen.getvalue()
        self.assertIn("» woke: sub-agent a3f2 finished", text)
        self.assertIn("→ read C:/notes.txt", text)
        self.assertIn("the sub-agent found 42", text)
        self.assertIn("a3f2  running  search", text)  # the toolbar line
        self.assertEqual(len(escapes), 1)
        self.assertFalse(cli._wake)
        self.assertIsNone(cli._above)


if __name__ == "__main__":
    unittest.main()
