from pathlib import Path
from typing import Optional
from .base import ToolResult


class EditTool:
    """Strict find-and-replace edit using absolute paths."""

    def __call__(self, path: str, oldText: str, newText: str) -> ToolResult:
        try:
            resolved = Path(path).resolve()

            if not resolved.exists():
                return ToolResult(False, "", f"File not found: {path}")

            content = resolved.read_text(encoding="utf-8", errors="replace")

            # Find all occurrences
            matches = []
            start = 0
            while True:
                idx = content.find(oldText, start)
                if idx == -1:
                    break
                matches.append(idx)
                start = idx + 1

            if len(matches) == 0:
                lines = content.split("\n")
                similar_lines = []
                for i, line in enumerate(lines, 1):
                    snippet = oldText[:20] if len(oldText) > 20 else oldText
                    if snippet in line:
                        similar_lines.append(i)

                error_msg = f"String not found in file. File has {len(lines)} lines."
                if similar_lines:
                    error_msg += f" Similar text found on line(s): {similar_lines[:5]}"
                return ToolResult(False, "", error_msg)

            if len(matches) > 1:
                line_positions = []
                for match_idx in matches:
                    line_num = content[:match_idx].count("\n") + 1
                    line_positions.append(line_num)

                return ToolResult(
                    False,
                    "",
                    f"Found {len(matches)} occurrences of the string (must be exactly 1). "
                    f"Found at line(s): {line_positions}",
                )

            # Exactly one match — perform replacement
            new_content = (
                content[: matches[0]]
                + newText
                + content[matches[0] + len(oldText) :]
            )
            resolved.write_text(new_content, encoding="utf-8")

            line_num = content[: matches[0]].count("\n") + 1
            return ToolResult(True, f"Successfully edited {path} at line {line_num}")
        except Exception as e:
            return ToolResult(False, "", f"Edit error: {str(e)}")
