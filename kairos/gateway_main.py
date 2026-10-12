"""Module entry point for the Kairos gateway."""
from __future__ import annotations

import logging
import sys

import uvicorn

from .config import Config
from .gateway.manager import GatewayManager
from .gateway.server import create_app
from .workspaces import WORKSPACE_ROOT


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    try:
        Config.validate()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        print("Create provider.json (see provider.example.json) before starting the gateway.")
        raise SystemExit(1) from exc

    workspace = sys.argv[1] if len(sys.argv) > 1 else Config.KAIROS_DEFAULT_WORKSPACE()
    host = Config.KAIROS_GATEWAY_HOST()
    port = Config.KAIROS_GATEWAY_PORT()
    gateway = GatewayManager(default_workspace=workspace)
    app = create_app(gateway)

    print("Kairos Gateway")
    print(f"  HTTP:      http://{host}:{port}")
    print(f"  WebSocket: ws://{host}:{port}/api/v1/ws")
    print(f"  Health:    http://{host}:{port}/healthz")
    print(f"  Database:  {app.state.gateway.repository.db_path}")
    print(f"  Workspace: new conversations without one get a fresh folder in {WORKSPACE_ROOT}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
