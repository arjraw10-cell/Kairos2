import bisect
import re
from typing import List, Optional, Tuple
from .base import ToolResult, resolve_path

# Typographic characters models often swap for their ASCII forms. Each maps
# one character to one character, so match positions stay the same.
_ASCII = str.maketrans({
    **dict.fromkeys("‘’‚‛", "'"),
    **dict.fromkeys("“”„‟", '"'),
    **dict.fromkeys("‐‑‒–—―−", "-"),
    **dict.fromkeys("            　", " "),
})
# The line-number prefix of read output: a right-aligned number and a tab.
_LINE_NUMBER = re.compile(r"^ *\d+\t")


def _to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _find_all(haystack: str, needle: str) -> List[int]:
    found = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return found
        found.append(idx)
        start = idx + 1


def _strip_trailing(text: str):
    """Remove trailing whitespace from every line. Returns the stripped text
    and a function mapping a position in it back to a position in ``text``."""
    lines = text.split("\n")
    bodies = [line.rstrip() for line in lines]
    starts, original_starts = [], []
    pos = original = 0
    for line, body in zip(lines, bodies):
        starts.append(pos)
        original_starts.append(original)
        pos += len(body) + 1
        original += len(line) + 1

    def to_original(p: int) -> int:
        i = bisect.bisect_right(starts, p) - 1
        offset = p - starts[i]
        return original_starts[i] + (offset if offset < len(bodies[i]) else len(lines[i]))

    return "\n".join(bodies), to_original


def _find_spans(content: str, old: str) -> Tuple[List[Tuple[int, int]], str]:
    """Spans of ``old`` in ``content`` from the first tier that finds any
    (exact, typographic characters normalized, trailing whitespace ignored),
    plus a note on how it matched ("" when exact)."""
    spans = [(i, i + len(old)) for i in _find_all(content, old)]
    if spans:
        return spans, ""
    content, old = content.translate(_ASCII), old.translate(_ASCII)
    spans = [(i, i + len(old)) for i in _find_all(content, old)]
    if spans:
        return spans, "after normalizing quotes, dashes and spaces"
    stripped, to_original = _strip_trailing(content)
    old = _strip_trailing(old)[0]
    if old.strip():
        spans = [(to_original(i), to_original(i + len(old) - 1) + 1) for i in _find_all(stripped, old)]
    return spans, "ignoring trailing whitespace"


def _strip_line_numbers(text: str) -> Optional[str]:
    """``text`` without read's line-number prefixes, or None unless every
    line has one."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if not lines or not all(_LINE_NUMBER.match(line) for line in lines):
        return None
    return "\n".join(_LINE_NUMBER.sub("", line, count=1) for line in text.split("\n"))


class EditTool:
    """Find-and-replace edit. Relative paths resolve against ``workspace``.

    ``oldText`` is matched exactly first; only when that finds nothing, with
    typographic quotes, dashes and spaces normalized, then with trailing
    whitespace ignored on each line, then all of that again without read's
    line-number prefixes. A fuzzy match still replaces only its exact span in
    the file. More than one match is refused unless ``replace_all``.

    The file is edited as raw UTF-8 text: its existing line endings, BOM and
    every byte outside the replaced spans are preserved. Files that are not
    valid UTF-8 are refused rather than rewritten with replacement characters.
    """

    MAX_REPORTED_MATCHES = 20

    def __init__(self, workspace: Optional[str] = None):
        self.workspace = workspace

    def __call__(self, path: str, oldText: str, newText: str, replace_all: bool = False) -> ToolResult:
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

            spans, how = _find_spans(content, oldText)
            if not spans:
                # oldText copied from read output with the line numbers still on.
                unnumbered = _strip_line_numbers(oldText)
                if unnumbered and unnumbered.strip():
                    spans, how = _find_spans(content, unnumbered)
                    how = "after removing line-number prefixes" + (f", {how}" if how else "")
                    newText = _strip_line_numbers(newText) or newText

            if len(spans) == 0:
                lines = content.split("\n")
                similar_lines = []
                for i, line in enumerate(lines, 1):
                    snippet = oldText[:20] if len(oldText) > 20 else oldText
                    if snippet in line:
                        similar_lines.append(i)

                error_msg = (
                    f"Could not find oldText in {path}. It must match the file exactly, including "
                    f"whitespace and indentation. Re-read the file and try again. File has {len(lines)} lines."
                )
                if similar_lines:
                    error_msg += f" Similar text found on line(s): {similar_lines[:5]}"
                return ToolResult(False, "", error_msg)

            if len(spans) > 1 and replace_all is not True:
                line_positions = []
                for start, _ in spans[: self.MAX_REPORTED_MATCHES]:
                    line_num = content[:start].count("\n") + 1
                    line_positions.append(line_num)

                return ToolResult(
                    False,
                    "",
                    f"Found {len(spans)} matches for oldText in {path}. Add more surrounding context "
                    f"to make it unique, or set replace_all=true. Found at line(s): {line_positions}",
                )

            # Replace every match in order, skipping one that overlaps the last.
            parts, end, replaced = [], 0, []
            for start, stop in spans:
                if start >= end:
                    parts += [content[end:start], newText]
                    replaced.append(start)
                    end = stop
            parts.append(content[end:])
            resolved.write_bytes("".join(parts).encode("utf-8"))

            line_num = content[: replaced[0]].count("\n") + 1
            note = f" (matched {how})" if how else ""
            if len(replaced) > 1:
                return ToolResult(True, f"Replaced {len(replaced)} occurrences in {path}, the first at line {line_num}{note}")
            return ToolResult(True, f"Successfully edited {path} at line {line_num}{note}")
        except Exception as e:
            return ToolResult(False, "", f"Edit error: {str(e)}")
