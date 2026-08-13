from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from app.routes import router  # noqa: E402  (must come after load_dotenv)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Unit Test Generator Agent", version="0.1.0")
app.include_router(router)


def run() -> None:
    """Entry point for the `testgen-server` script."""
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
