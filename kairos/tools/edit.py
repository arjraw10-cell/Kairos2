from typing import Optional
from .base import ToolResult, resolve_path


def _to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


class EditTool:
    """Strict find-and-replace edit. Relative paths resolve against ``workspace``.

    The file is edited as raw UTF-8 text: its existing line endings and every
    byte outside the replaced span are preserved. Files that are not valid
    UTF-8 are refused rather than rewritten with replacement characters.
    """

    MAX_REPORTED_MATCHES = 20

    def __init__(self, workspace: Optional[str] = None):
        self.workspace = workspace

    def __call__(self, path: str, oldText: str, newText: str) -> ToolResult:
        try:
            resolved = resolve_path(path, self.workspace)

            if not resolved.exists():
                return ToolResult(False, "", f"File not found: {path}")
            if not oldText:
                return ToolResult(False, "", "oldText must not be empty.")

            try:
                content = resolved.read_bytes().decode("utf-8")
            except UnicodeDecodeError as e:
                return ToolResult(
                    False, "",
                    f"{path} is not valid UTF-8 ({e.reason} at byte {e.start}); "
                    "edit refuses to rewrite it because that would corrupt its bytes.",
                )

            # The model writes "\n"; in a file that mostly uses CRLF, match and
            # insert CRLF so the file keeps its line endings.
            crlf = content.count("\r\n")
            if crlf > content.count("\n") - crlf:
                oldText, newText = _to_crlf(oldText), _to_crlf(newText)

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
                for match_idx in matches[: self.MAX_REPORTED_MATCHES]:
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
            resolved.write_bytes(new_content.encode("utf-8"))

            line_num = content[: matches[0]].count("\n") + 1
            return ToolResult(True, f"Successfully edited {path} at line {line_num}")
        except Exception as e:
            return ToolResult(False, "", f"Edit error: {str(e)}")
