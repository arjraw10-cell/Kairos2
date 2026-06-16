import base64
from pathlib import Path
from .base import ToolResult

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.tif', '.svg'}

MIME_MAP = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
    '.tiff': 'image/tiff',
    '.tif': 'image/tiff',
    '.svg': 'image/svg+xml',
}


class ReadTool:
    """Read file contents using absolute paths. Supports images (returned as visual data)."""

    def __call__(self, path: str) -> ToolResult:
        try:
            resolved = Path(path).resolve()

            if not resolved.exists():
                return ToolResult(False, "", f"File not found: {path}")
            if not resolved.is_file():
                return ToolResult(False, "", f"Not a file: {path}")

            is_image = resolved.suffix.lower() in IMAGE_EXTENSIONS
            max_size = 20 * 1024 * 1024 if is_image else 100 * 1024  # 20MB images, 100KB text
            size_bytes = resolved.stat().st_size

            if size_bytes > max_size:
                limit_str = f">{max_size // (1024 * 1024)}MB" if is_image else f">{max_size // 1024}KB"
                return ToolResult(False, "", f"File too large ({limit_str}): {path}")

            if is_image:
                mime = MIME_MAP.get(resolved.suffix.lower(), 'image/png')
                data = resolved.read_bytes()
                b64 = base64.b64encode(data).decode('ascii')
                data_url = f"data:{mime};base64,{b64}"
                return ToolResult(
                    True,
                    f"Image file: {path} ({mime}, {size_bytes:,} bytes)",
                    image_url=data_url,
                )

            content = resolved.read_text(encoding="utf-8", errors="replace")
            return ToolResult(True, content)
        except Exception as e:
            return ToolResult(False, "", f"Read error: {str(e)}")
