import os
import re
import fnmatch
from pathlib import Path
from typing import Optional
from .base import ToolResult


class SearchTool:
    """Search file contents using regex, like ripgrep."""

    # Directories to always skip
    SKIP_DIRS = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".eggs",
    }

    def __call__(
        self,
        pattern: str,
        path: Optional[str] = None,
        include: Optional[str] = None,
        max_results: int = 50,
    ) -> ToolResult:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return ToolResult(False, "", f"Invalid regex: {e}")

        search_root = Path(path).resolve() if path else Path.cwd()
        if not search_root.exists():
            return ToolResult(False, "", f"Path not found: {path}")
        if not search_root.is_dir():
            return ToolResult(False, "", f"Not a directory: {path}")

        # Build include filter — convert glob-like patterns to regex
        include_re = None
        if include:
            # Convert simple glob: *.py -> .*\.py, *.js -> .*\.js
            regex_pattern = fnmatch.translate(include)
            try:
                include_re = re.compile(regex_pattern, re.IGNORECASE)
            except re.error:
                include_re = re.compile(re.escape(include), re.IGNORECASE)

        matches = []
        files_searched = 0

        for root, dirs, files in os.walk(search_root):
            # Prune skipped directories
            dirs[:] = [
                d for d in dirs
                if d not in self.SKIP_DIRS and not d.endswith(".egg-info")
            ]

            for fname in files:
                if include_re and not include_re.search(fname):
                    continue

                fpath = Path(root) / fname

                # Skip binary files (quick check: try reading a small chunk)
                try:
                    with open(fpath, "rb") as fb:
                        chunk = fb.read(512)
                        if b"\x00" in chunk:
                            continue
                except (OSError, PermissionError):
                    continue

                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except (OSError, PermissionError):
                    continue

                files_searched += 1
                for line_num, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        rel = fpath.relative_to(search_root)
                        matches.append({
                            "file": str(rel),
                            "line": line_num,
                            "text": line.rstrip(),
                        })
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolResult(
                True,
                f"No matches for '{pattern}' in {search_root} ({files_searched} files searched)",
            )

        # Format output
        lines = []
        for m in matches:
            lines.append(f"{m['file']}:{m['line']}: {m['text']}")
        output = "\n".join(lines)

        truncated = ""
        if len(matches) >= max_results:
            truncated = f" (showing first {max_results})"

        return ToolResult(True, f"{len(matches)} matches{truncated}:\n{output}")
