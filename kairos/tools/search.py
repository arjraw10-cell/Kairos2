import fnmatch
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

from .base import ToolResult


class SearchTool:
    """Search file contents using regex, like ripgrep.

    The search is deliberately bounded so a large workspace or a slow mounted
    filesystem cannot keep an agent tool call running indefinitely. Timeout
    checks are made between files and while reading lines, allowing partial
    results to be returned when the deadline is reached.
    """

    DEFAULT_TIMEOUT = 10.0

    # Directories to always skip
    SKIP_DIRS = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".eggs",
    }

    @staticmethod
    def _format_matches(matches: list[dict], max_results: int) -> str:
        """Format collected matches using the normal search output format."""
        lines = [f"{m['file']}:{m['line']}: {m['text']}" for m in matches]
        output = "\n".join(lines)
        truncated = f" (showing first {max_results})" if len(matches) >= max_results else ""
        return f"{len(matches)} matches{truncated}:\n{output}"

    @staticmethod
    def _timeout_result(
        pattern: str,
        search_root: Path,
        files_searched: int,
        matches: list[dict],
        timeout: float,
        max_results: int,
    ) -> ToolResult:
        """Return a failed result while preserving any matches found so far."""
        message = (
            f"Search timed out after {timeout:g} seconds "
            f"({files_searched} files searched)"
        )
        if matches:
            output = f"{message}; partial results for '{pattern}' in {search_root}:\n"
            output += SearchTool._format_matches(matches, max_results)
        else:
            output = message
        return ToolResult(False, output, message)

    def __call__(
        self,
        pattern: str,
        path: Optional[str] = None,
        include: Optional[str] = None,
        max_results: int = 50,
        timeout: Optional[float] = DEFAULT_TIMEOUT,
    ) -> ToolResult:
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT
        if isinstance(timeout, bool):
            return ToolResult(False, "", "Search timeout must be a finite non-negative number")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return ToolResult(False, "", "Search timeout must be a finite non-negative number")
        if not math.isfinite(timeout) or timeout < 0:
            return ToolResult(False, "", "Search timeout must be a finite non-negative number")
        deadline = time.monotonic() + timeout

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return ToolResult(False, "", f"Invalid regex: {e}")

        search_root = Path(path).resolve() if path else Path.cwd()
        if not search_root.exists():
            return ToolResult(False, "", f"Path not found: {path}")
        if not search_root.is_dir():
            return ToolResult(False, "", f"Not a directory: {path}")
        if isinstance(max_results, bool):
            return ToolResult(False, "", "max_results must be a positive integer")
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            return ToolResult(False, "", "max_results must be a positive integer")
        if max_results < 1:
            return ToolResult(False, "", "max_results must be a positive integer")
        if time.monotonic() >= deadline:
            return self._timeout_result(pattern, search_root, 0, [], timeout, max_results)

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
            if time.monotonic() >= deadline:
                return self._timeout_result(
                    pattern, search_root, files_searched, matches, timeout, max_results
                )

            # Prune skipped directories
            dirs[:] = [
                d for d in dirs
                if d not in self.SKIP_DIRS and not d.endswith(".egg-info")
            ]

            for fname in files:
                if time.monotonic() >= deadline:
                    return self._timeout_result(
                        pattern, search_root, files_searched, matches, timeout, max_results
                    )
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

                if time.monotonic() >= deadline:
                    return self._timeout_result(
                        pattern, search_root, files_searched, matches, timeout, max_results
                    )

                files_searched += 1
                try:
                    # Read line-by-line instead of loading the whole file so
                    # large files can observe the deadline while being scanned.
                    with open(fpath, "r", encoding="utf-8", errors="replace") as text_file:
                        for line_num, line in enumerate(text_file, 1):
                            if time.monotonic() >= deadline:
                                return self._timeout_result(
                                    pattern,
                                    search_root,
                                    files_searched,
                                    matches,
                                    timeout,
                                    max_results,
                                )
                            if regex.search(line):
                                rel = fpath.relative_to(search_root)
                                matches.append({
                                    "file": str(rel),
                                    "line": line_num,
                                    "text": line.rstrip(),
                                })
                                if len(matches) >= max_results:
                                    break
                except (OSError, PermissionError):
                    continue

                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolResult(
                True,
                f"No matches for '{pattern}' in {search_root} ({files_searched} files searched)",
            )

        return ToolResult(True, self._format_matches(matches, max_results))
