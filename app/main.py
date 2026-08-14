import logging

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .routes.extract import router as extract_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Receipt Extractor API")
app.include_router(extract_router)


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
