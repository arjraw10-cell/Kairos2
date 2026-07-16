"""Kairos Gateway entry point — starts the FastAPI WebSocket server.

Usage:
    python -m kairos.main_gateway [default_workspace]

Or:
    kairos serve [default_workspace]

If no default_workspace is provided and KAIROS_DEFAULT_WORKSPACE is not
set in .env, the gateway starts with no default — each client must
specify a workspace when creating a new session.
"""
import logging
import os
import sys

import uvicorn

from kairos.config import Config
from kairos.gateway.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    try:
        Config.validate()
    except ValueError as e:
        print(f"Configuration error: {e}")
        print("Please create a .env file with your OPENAI_API_KEY.")
        sys.exit(1)

    workspace = sys.argv[1] if len(sys.argv) > 1 else Config.KAIROS_DEFAULT_WORKSPACE()
    port = Config.KAIROS_GATEWAY_PORT()
    host = Config.KAIROS_GATEWAY_HOST()

    app = create_app(default_workspace=workspace)

    print("Kairos Gateway")
    print(f"  URL:      ws://{host}:{port}/ws")
    print(f"  Health:   http://{host}:{port}/health")
    print(f"  Sessions: http://{host}:{port}/api/sessions")
    if workspace:
        print(f"  Default:  {workspace}")
    else:
        print("  Default:  (none — clients choose workspace)")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
