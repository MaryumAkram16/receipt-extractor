import json
import logging

from fastapi import APIRouter, HTTPException

from ..llm.errors import ErrorCategory, classify
from ..llm.schema import ExtractRequest, ExtractResult

router = APIRouter()
logger = logging.getLogger("routes.extract")


@router.post("/extract", response_model=ExtractResult)
def extract_endpoint(req: ExtractRequest):
    from ..llm.parse import run_extraction  # deferred import: skip client init in stub/kill-switch paths

    try:
        return run_extraction(req.text)
    except Exception as exc:  # noqa: BLE001 — mapped to a safe public error below
        info = classify(exc)
        if info.category is ErrorCategory.INTERNAL:
            raise  # a genuine bug: FastAPI answers a bare 500 and logs the traceback
        # Callers get a fixed message per category (504 timeout, 503 rate
        # limited / not configured, 502 other provider failures, 422 output
        # that failed validation after repair) — never provider error bodies
        # or fragments of model output.
        logger.warning(json.dumps({
            "event": "extract_failed",
            "error_category": info.category.value,
            "upstream_status": info.upstream_status,
            "http_status": info.http_status,
        }))
        raise HTTPException(status_code=info.http_status, detail=info.public_message) from None
