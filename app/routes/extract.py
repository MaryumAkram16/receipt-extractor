from fastapi import APIRouter, HTTPException

from ..llm.schema import ExtractRequest, ExtractResult

router = APIRouter()


@router.post("/extract", response_model=ExtractResult)
def extract_endpoint(req: ExtractRequest):
    from ..llm.parse import run_extraction  # deferred import: skip client init in stub/kill-switch paths
    from openai import APITimeoutError

    try:
        return run_extraction(req.text)
    except APITimeoutError:
        raise HTTPException(status_code=504, detail="model call timed out")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))