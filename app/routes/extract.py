import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..llm.schema import ExtractRequest, ExtractResult

router = APIRouter()

STUB_RESULT = ExtractResult(
    vendor="Stub Cafe",
    date="2025-01-01",
    total_amount=100.0,
    currency="PKR",
    confidence=0.9,
    needs_review=False,
)

FALLBACK_RESULT = ExtractResult(
    vendor=None,
    date=None,
    total_amount=None,
    currency=None,
    confidence=0.0,
    needs_review=True,
)


@router.post("/extract", response_model=ExtractResult)
def extract_endpoint(req: ExtractRequest):
    # Kill switch first: if the feature is disabled, never touch the model,
    # never touch stub mode — just return a safe, deterministic fallback.
    if os.environ.get("LLM_ENABLED", "true").lower() == "false":
        return FALLBACK_RESULT

    if os.environ.get("LLM_STUB") == "1":
        return STUB_RESULT

    from ..llm.parse import extract  # deferred import: skip client init in stub/kill-switch paths
    from openai import APITimeoutError

    try:
        return extract(req.text)
    except APITimeoutError:
        raise HTTPException(status_code=504, detail="model call timed out")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
