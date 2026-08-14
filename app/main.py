import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .routes.extract import router as extract_router
from .routes.jobs import router as jobs_router
from .jobs.worker import start_workers

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("app")


def _validate_config() -> None:
    enabled = os.environ.get("LLM_ENABLED", "true").lower()
    stub = os.environ.get("LLM_STUB", "0") == "1"

    if enabled == "false" or stub:
        return  # kill switch or stub mode: no real model call will be made

    missing: list[str] = []
    if not os.environ.get("LLM_API_KEY"):
        missing.append("LLM_API_KEY")
    if not os.environ.get("LLM_BASE_URL"):
        missing.append("LLM_BASE_URL")
    if not os.environ.get("LLM_MODEL"):
        missing.append("LLM_MODEL")

    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + " — see .env.example"
        )


app = FastAPI(title="Receipt Extractor API")
app.include_router(extract_router)
app.include_router(jobs_router)


@app.on_event("startup")
def _on_startup():
    _validate_config()
    start_workers(count=2)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Input validation failures are a 400 naming the offending field, not
    # FastAPI's default 422 — this is a request we reject before spending
    # a single model call.
    errors = exc.errors()
    field = ".".join(str(p) for p in errors[0]["loc"][1:]) if errors else "unknown"
    message = errors[0]["msg"] if errors else "invalid request"
    return JSONResponse(status_code=400, content={"error": f"{field}: {message}"})


@app.get("/health")
def health():
    return {"status": "ok"}