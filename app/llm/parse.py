"""Parse a model's raw text into ExtractResult, with one repair retry.

The model's answer is untrusted input, exactly like anything else that
arrives from outside the system. It goes through the same pipeline as any
other external data: parse, validate, and if it fails, quarantine rather
than trust it or crash.
"""
import json
import os
import re

from pydantic import ValidationError

from .client import call_model, load_prompt, quarantine
from .schema import ExtractResult
from .. import db

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


def run_extraction(user_text: str) -> ExtractResult:
    """The single entry point every caller (sync route, job worker) should
    use — it's the one place stub mode and the kill switch are checked, so
    neither path can accidentally make a real call when it shouldn't.
    """
    if os.environ.get("LLM_ENABLED", "true").lower() == "false":
        return FALLBACK_RESULT
    if os.environ.get("LLM_STUB") == "1":
        return STUB_RESULT

    result = extract(user_text)
    # Only real model extractions get persisted — stub and kill-switch
    # results are fake by construction and would pollute the report's numbers.
    db.insert_extraction(
        vendor=result.vendor,
        date=result.date,
        total_amount=result.total_amount,
        currency=result.currency.value if result.currency else None,
        confidence=result.confidence,
        needs_review=result.needs_review,
        source="api",
    )
    return result


def _strip_fence(text: str) -> str:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def extract(user_text: str) -> ExtractResult:
    system_prompt = load_prompt()
    raw, _, _ = call_model(system_prompt, user_text)

    result, error = _try_parse(raw)
    if result is not None:
        return result

    # One repair retry: hand the model its own broken output and the exact
    # validation error, and ask for a corrected version only.
    repair_user_msg = (
        f"Original input:\n{user_text}\n\n"
        f"Your previous answer was rejected for this reason: {error}\n"
        f"Previous answer: {raw}\n"
        "Return only corrected JSON matching the schema."
    )
    raw2, _, _ = call_model(system_prompt, repair_user_msg, repair=True)
    result2, error2 = _try_parse(raw2)
    if result2 is not None:
        return result2

    quarantine(user_text, raw2, error2 or "unknown parse/validation error")
    raise ValueError(error2 or "model output failed validation after repair")


def _try_parse(raw: str):
    candidate = _strip_fence(raw)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"

    try:
        return ExtractResult.model_validate(data), None
    except ValidationError as exc:
        return None, str(exc)