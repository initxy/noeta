"""``python -m noeta.agent`` — the only entry point.

Zero-argument and env-only: configuration comes from .env / environment
variables (see config.py). Builds the FastAPI app and serves it with uvicorn.
"""
from __future__ import annotations

import uvicorn

from noeta.agent.config import get_settings
from noeta.agent.main import create_app


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
