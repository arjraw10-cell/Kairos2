import subprocess
from typing import Optional
from .base import ToolResult


class GitTool:
    """Run git commands and return structured output."""

    def __init__(self):
        self._workspace = None

    def set_workspace(self, path: str):
        self._workspace = path

    def _run(self, args: list[str], timeout: int = 10) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self._workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout + result.stderr).strip()
            return result.returncode == 0, output
        except FileNotFoundError:
            return False, "git is not installed or not in PATH"
        except subprocess.TimeoutExpired:
            return False, f"git command timed out after {timeout}s"
        except Exception as e:
            return False, str(e)

    def status(self) -> ToolResult:
        """Show working tree status."""
        ok, output = self._run(["status", "--porcelain"])
        if not ok:
            return ToolResult(False, "", output)
        if not output:
            return ToolResult(True, "Working tree clean — no changes.")
        return ToolResult(True, output)

    def diff(self, path: Optional[str] = None) -> ToolResult:
        """Show changes in working tree."""
        args = ["diff"]
        if path:
            args = ["diff", "--", path]
        ok, output = self._run(args)
        if not ok:
            return ToolResult(False, "", output)
        if not output:
            return ToolResult(True, "No changes (or all changes staged).")
        return ToolResult(True, output)

    def log(self, count: int = 10) -> ToolResult:
        """Show recent commits."""
        args = ["log", f"--oneline", f"-n", str(count)]
        ok, output = self._run(args)
        if not ok:
            return ToolResult(False, "", output)
        if not output:
            return ToolResult(True, "No commits yet.")
        return ToolResult(True, output)

    def commit(self, message: str) -> ToolResult:
        """Stage all changes and commit."""
        # Stage everything
        ok, out = self._run(["add", "-A"])
        if not ok:
            return ToolResult(False, "", f"git add failed: {out}")

        ok, out = self._run(["commit", "-m", message])
        if not ok:
            return ToolResult(False, "", out)
        return ToolResult(True, out)

    def branch(self) -> ToolResult:
        """List branches."""
        ok, output = self._run(["branch", "--list"])
        if not ok:
            return ToolResult(False, "", output)
        return ToolResult(True, output or "No branches.")

    def __call__(self, command: str, **kwargs) -> ToolResult:
        """Dispatch to the right sub-command."""
        cmd = command.strip().lower()

        if cmd == "status":
            return self.status()
        elif cmd == "diff":
            return self.diff(kwargs.get("path"))
        elif cmd == "log":
            return self.log(kwargs.get("count", 10))
        elif cmd == "commit":
            message = kwargs.get("message", "")
            if not message:
                return ToolResult(False, "", "commit requires a 'message' argument")
            return self.commit(message)
        elif cmd == "branch":
            return self.branch()
        else:
            return ToolResult(
                False,
                "",
                f"Unknown git command: {command}. Use: status, diff, log, commit, branch",
            )
