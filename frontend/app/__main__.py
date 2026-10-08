"""Run the invoice UI service.

Binds every interface by default so the UI is reachable from any IP, not just
127.0.0.1. Override HOST/PORT to change where it listens.
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
