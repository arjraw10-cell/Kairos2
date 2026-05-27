import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import asyncio
from textual.app import App as TextualApp, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, Markdown, Static
from textual import work

from src.agent import Agent

class ChatMessage(Static):
    def __init__(self, role: str, content: str = ""):
        super().__init__()
        self.role = role
        self.content = content
        self.md = Markdown("")

    def compose(self) -> ComposeResult:
        yield self.md

    def on_mount(self) -> None:
        self.update_markdown()

    def update_content(self, new_content: str) -> None:
        self.content += new_content
        self.update_markdown()

    def update_markdown(self) -> None:
        prefix = "**User:** " if self.role == "user" else "**Agent:** "
        self.md.update(prefix + self.content)

class App(TextualApp):
    CSS = """
    #chat-container {
        height: 1fr;
        padding: 1;
    }
    #input-box {
        dock: bottom;
        margin: 1;
    }
    ChatMessage {
        margin-bottom: 1;
        padding: 1;
        background: $boost;
        height: auto;
    }
    """

    def __init__(self):
        super().__init__()
        self.agent = Agent()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chat-container"):
            pass
        yield Input(placeholder="Type a message and press Enter...", id="input-box")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        user_message = event.value
        if not user_message.strip():
            return

        input_widget = self.query_one("#input-box", Input)
        input_widget.value = ""

        chat_container = self.query_one("#chat-container", VerticalScroll)

        # Display user message
        user_msg_widget = ChatMessage("user", user_message)
        await chat_container.mount(user_msg_widget)
        chat_container.scroll_end(animate=False)

        # Display empty agent message bubble
        agent_msg_widget = ChatMessage("agent", "")
        await chat_container.mount(agent_msg_widget)
        chat_container.scroll_end(animate=False)

        # Call agent in background worker
        self.run_agent_task(user_message, agent_msg_widget)

    @work
    async def run_agent_task(self, user_message: str, agent_msg_widget: ChatMessage) -> None:

        # We can pass an async callback so that it safely updates the UI on the event loop
        async def yield_callback(token: str):
            # Using call_from_thread as requested, although in an async worker
            # it might be in the same thread. call_from_thread ensures thread safety.
            def update_ui():
                agent_msg_widget.update_content(token)
                # Scroll to end
                chat_container = self.query_one("#chat-container", VerticalScroll)
                chat_container.scroll_end(animate=False)

            self.call_from_thread(update_ui)

        await self.agent.process_user_message(user_message, yield_callback)


if __name__ == "__main__":
    App().run()
