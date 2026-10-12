"""Fresh per-conversation workspace folders."""
from datetime import datetime
from pathlib import Path

WORKSPACE_ROOT = Path.home() / "Documents" / "kairos_workspace"


def new_workspace() -> Path:
    """Create and return a new folder named by local time, e.g. 2026-10-11_143005."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    path = WORKSPACE_ROOT / stamp
    suffix = 2
    while True:
        try:
            path.mkdir()
            return path
        except FileExistsError:
            path = WORKSPACE_ROOT / f"{stamp}_{suffix}"
            suffix += 1
