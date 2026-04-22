"""Boot the demo API with uvicorn.

Defaults to host=127.0.0.1, port=8000 — same defaults the
reviewer UI's dev server expects. Override via env:

    HOST=0.0.0.0 PORT=8001 uv run python scripts/serve.py

For prod-style boot, call uvicorn directly:

    uv run uvicorn clinical_demo.api:create_app --factory --port 8000
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "clinical_demo.api:create_app",
        factory=True,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
