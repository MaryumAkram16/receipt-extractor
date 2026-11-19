"""Parse a model's raw text into ExtractResult, with one repair retry.

The model's answer is untrusted input, exactly like anything else that
arrives from outside the system. It goes through the same pipeline as any
other external data: parse, validate, and if it fails, quarantine rather
than trust it or crash.
"""
import json
import re

from pydantic import ValidationError

from .client import call_model, load_prompt, quarantine
from .errors import InvalidModelOutput
from .schema import ExtractResult
from .. import db
from ..config import llm_enabled, llm_stub

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
    if not llm_enabled():
        return FALLBACK_RESULT.model_copy()
    if llm_stub():
        return STUB_RESULT.model_copy()

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
    # Generic on purpose: error2 can quote the model's output, which may
    # contain receipt contents. Details live in the quarantine record.
    raise InvalidModelOutput("model output failed validation after repair")


def describe_validation_error(exc: ValidationError) -> str:
    """Field path + message for each error, *without* the offending input
    value. Pydantic's default str(exc) embeds `input_value=...`, i.e. a slice
    of the model's output, which must not reach logs or API responses."""
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "schema validation failed: " + "; ".join(parts)


def _try_parse(raw: str):
    candidate = _strip_fence(raw or "")
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        # JSONDecodeError's message is position-only ("Expecting value: line 1
        # column 1 (char 0)") and never quotes the document.
        return None, f"invalid JSON: {exc}"

    if not isinstance(data, dict):
        return None, f"invalid JSON: expected an object, got {type(data).__name__}"

    try:
        return ExtractResult.model_validate(data), None
    except ValidationError as exc:
        return None, describe_validation_error(exc)