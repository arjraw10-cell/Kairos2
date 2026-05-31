from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.live import Live
from rich.text import Text
from prompt_toolkit import prompt
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from typing import Optional
import sys

console = Console()

# Custom style for prompts
PROMPT_STYLE = Style.from_dict({
    'prompt': 'ansicyan bold',
    '': 'ansidefault',
})

class CLI:
    def __init__(self):
        self.console = console

    def print_banner(self):
        """Print the kairos banner"""
        banner = Text()
        banner.append("█▀▄▀█ █▀█ █▀▄ █▀▀ █▀█ █▀▄▀█\n█░▀░█ █▄█ █▄▀ ██▄ █▄█ █░▀░█", style="bold cyan")
        banner.append("\n", style="dim")
        banner.append("Minimal Coding Agent", style="italic white")
        
        self.console.print(Panel(banner, border_style="cyan", padding=(1, 2)))
        self.console.print()

    def print_thinking(self, text: str = "Thinking..."):
        """Show thinking indicator"""
        self.console.print(f"[dim italic]{text}[/dim italic]")

    def print_tool_call(self, tool_name: str, args: dict):
        """Display a tool call being made"""
        args_str = ", ".join(f"{k}=[yellow]{v}[/yellow]" for k, v in args.items())
        self.console.print(f"[blue]→[/blue] [bold]{tool_name}[/bold]({args_str})")

    def print_tool_result(self, success: bool, output: str, error: Optional[str] = None):
        """Display tool execution result"""
        if success:
            if output and len(output) < 200:
                self.console.print(f"[green]✓[/green] {output}")
            else:
                self.console.print(f"[green]✓[/green] Command executed")
        else:
            error_msg = error or output
            self.console.print(Panel(f"[red]{error_msg}[/red]", border_style="red", title="Tool Error"))

    def print_response(self, content: str):
        """Print the agent's final response"""
        # Try to render as markdown, fall back to plain text
        try:
            md = Markdown(content)
            self.console.print(Panel(md, border_style="green", padding=(1, 2)))
        except:
            self.console.print(Panel(content, border_style="green", padding=(1, 2)))

    def print_code(self, code: str, language: str = "python"):
        """Display code with syntax highlighting"""
        syntax = Syntax(code, language, theme="monokai", line_numbers=True)
        self.console.print(syntax)

    def get_user_input(self, prefix: str = "kairos> ") -> Optional[str]:
        """Get user input with styled prompt"""
        try:
            return prompt(
                HTML(f'<style class="prompt">{prefix}</style>'),
                style=PROMPT_STYLE
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    def print_error(self, message: str):
        """Print an error message"""
        self.console.print(f"[red]Error:[/red] {message}")

    def print_info(self, message: str):
        """Print an info message"""
        self.console.print(f"[cyan]Info:[/cyan] {message}")

    def print_workspace(self, path: str):
        """Display current workspace"""
        self.console.print(f"[dim]Workspace:[/dim] [bold cyan]{path}[/bold cyan]")

    def clear_screen(self):
        """Clear the terminal"""
        self.console.clear()

    def print_exit(self):
        """Print exit message"""
        self.console.print("\n[dim]Goodbye![/dim]\n")