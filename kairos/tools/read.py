import base64
from typing import Optional
from .base import ToolResult, resolve_path

# Formats that both OpenAI and Anthropic accept as image input.
SUPPORTED_IMAGES = "PNG, JPEG, GIF and WebP"
# Extensions that mean "this is an image" even when the content is not a
# supported format (so a .bmp is refused instead of being read as text).
# SVG is XML text and is read as text.
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.tif'}
# Anthropic rejects images whose base64 data exceeds 5 MB; 3/4 of that is the
# largest file that stays under the limit once encoded.
MAX_IMAGE_BYTES = 5 * 1024 * 1024 * 3 // 4
# One page of a text file: at most this many lines and characters (the
# caller lowers the character budget to its tool-result cap).
MAX_LINES = 2000
MAX_PAGE_CHARS = 50_000
MAX_LINE_CHARS = 2000
# Room kept for the footer inside the character budget.
FOOTER_RESERVE = 200


def _image_mime(header: bytes) -> Optional[str]:
    """Return the MIME type of a supported image from its magic bytes."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


class ReadTool:
    """Read file contents. Supports images (returned as visual data).

    Text is returned one page at a time, each line prefixed with its 1-based
    number and a tab (like ``cat -n``), followed by a footer that says how to
    continue. Relative paths resolve against ``workspace`` when one is given.
    """

    def __init__(self, workspace: Optional[str] = None):
        self.workspace = workspace

    def __call__(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        max_chars: int = MAX_PAGE_CHARS,
    ) -> ToolResult:
        try:
            resolved = resolve_path(path, self.workspace)

            if not resolved.exists():
                return ToolResult(False, "", f"File not found: {path}")
            if not resolved.is_file():
                return ToolResult(False, "", f"Not a file: {path}")

            size_bytes = resolved.stat().st_size
            with open(resolved, "rb") as f:
                mime = _image_mime(f.read(12))

            if mime is None and resolved.suffix.lower() in IMAGE_EXTENSIONS:
                return ToolResult(
                    False, "",
                    f"Unsupported image: {path}. Only {SUPPORTED_IMAGES} images can be "
                    "read (detected from the file's content); convert it first.",
                )

            if mime:
                if size_bytes > MAX_IMAGE_BYTES:
                    return ToolResult(
                        False, "",
                        f"Image too large ({size_bytes:,} bytes; max {MAX_IMAGE_BYTES:,} "
                        f"so the encoded image stays under 5 MB): {path}. Resize or compress it first.",
                    )
                b64 = base64.b64encode(resolved.read_bytes()).decode('ascii')
                return ToolResult(
                    True,
                    f"Image file: {path} ({mime}, {size_bytes:,} bytes)",
                    image_url=f"data:{mime};base64,{b64}",
                )

            start = 1 if offset is None else int(offset)
            count = MAX_LINES if limit is None else int(limit)
            if start < 1:
                return ToolResult(False, "", f"offset must be 1 or more (it is the first line to read), got {start}.")
            if count < 1:
                return ToolResult(False, "", f"limit must be 1 or more, got {count}.")
            return self._read_page(resolved, path, start, min(count, MAX_LINES), min(max_chars, MAX_PAGE_CHARS))
        except Exception as e:
            return ToolResult(False, "", f"Read error: {str(e)}")

    @staticmethod
    def _read_page(resolved, path: str, start: int, count: int, budget: int) -> ToolResult:
        """Number the lines from ``start``; stop at ``count`` lines or ``budget`` chars."""
        out = []
        used = FOOTER_RESERVE
        total = 0
        next_line = None  # first line not shown, when the page stops early
        with open(resolved, "rb") as f:
            for total, raw in enumerate(f, 1):
                if total < start or next_line is not None:
                    continue
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if len(line) > MAX_LINE_CHARS:
                    line = line[:MAX_LINE_CHARS] + f" ... [line truncated: {len(line):,} chars]"
                numbered = f"{total:>6}\t{line}\n"
                if len(out) == count or (out and used + len(numbered) > budget):
                    next_line = total
                    continue
                out.append(numbered)
                used += len(numbered)

        if start > max(total, 1):
            return ToolResult(
                False, "",
                f"offset {start} is past the end of {path} ({total} lines). Use an offset from 1 to {max(total, 1)}.",
            )
        if next_line is not None:
            footer = (f"[Showing lines {start}-{next_line - 1} of {total}. "
                      f"Use offset={next_line} to continue.]")
        else:
            footer = f"[End of file — {total} lines]"
        return ToolResult(True, "".join(out) + footer)
