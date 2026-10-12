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
MAX_TEXT_BYTES = 100 * 1024


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

    Relative paths resolve against ``workspace`` when one is given.
    """

    def __init__(self, workspace: Optional[str] = None):
        self.workspace = workspace

    def __call__(self, path: str) -> ToolResult:
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

            if size_bytes > MAX_TEXT_BYTES:
                return ToolResult(False, "", f"File too large (>{MAX_TEXT_BYTES // 1024}KB): {path}")

            content = resolved.read_text(encoding="utf-8", errors="replace")
            return ToolResult(True, content)
        except Exception as e:
            return ToolResult(False, "", f"Read error: {str(e)}")
