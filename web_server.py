"""Project Infinity web client — server entrypoint.

Runs the FastAPI app from the repo root so relative paths (output/, config/,
GameMaster_MCP.md, dice_server.py) resolve correctly.

Usage:
    venv\\Scripts\\python.exe web_server.py
    venv\\Scripts\\python.exe web_server.py --port 8000 --reload
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Project Infinity web client server")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--log-level", default="info", help="uvicorn log level (default: info)")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
