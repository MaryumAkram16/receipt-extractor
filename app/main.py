import logging

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .routes.extract import router as extract_router
from .routes.jobs import router as jobs_router
from .routes.reports import router as reports_router
from .jobs.worker import start_workers
from .db import init_db
from .scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Receipt Extractor API")
app.include_router(extract_router)
app.include_router(jobs_router)
app.include_router(reports_router)


@app.on_event("startup")
def _startup():
    init_db()
    start_workers(count=2)
    start_scheduler()


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