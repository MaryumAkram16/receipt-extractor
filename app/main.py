import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import llm_mode
from .llm.client import PROMPT_VERSION
from .routes.extract import router as extract_router
from .routes.jobs import router as jobs_router
from .routes.reports import router as reports_router
from .jobs.worker import start_workers
from .db import init_db
from .scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    start_workers(count=2)
    start_scheduler()
    yield


app = FastAPI(title="Receipt Extractor API", lifespan=lifespan)
app.include_router(extract_router)
app.include_router(jobs_router)
app.include_router(reports_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Input validation failures are a 400 naming the offending field, not
    # FastAPI's default 422 — this is a request we reject before spending
    # a single model call. Only the field path and message are returned;
    # the submitted value is never echoed back.
    errors = exc.errors()
    if not errors:
        return JSONResponse(status_code=400, content={"error": "invalid request"})
    first = errors[0]
    field = ".".join(str(p) for p in first.get("loc", ())[1:]) or "body"
    message = first.get("msg", "invalid request")
    # Pydantic prefixes errors raised by custom validators with "Value error, ".
    message = message.removeprefix("Value error, ")
    return JSONResponse(status_code=400, content={"error": f"{field}: {message}"})


@app.get("/health")
def health():
    # llm_mode ("live" | "stub" | "disabled") lets evaluation tooling refuse
    # to score a server that isn't actually calling a model. prompt_version
    # and model let an evaluation report record what was actually running.
    # The model name is only reported in live mode (it isn't used otherwise)
    # and is never a secret; the API key is never exposed.
    mode = llm_mode()
    model = os.environ.get("LLM_MODEL", "").strip() or None
    return {
        "status": "ok",
        "llm_mode": mode,
        "prompt_version": PROMPT_VERSION,
        "model": model if mode == "live" else None,
    }
