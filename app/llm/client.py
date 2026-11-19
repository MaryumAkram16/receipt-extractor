"""Thin wrapper around the OpenAI-compatible client.

Owns the four things a real LLM call needs that a chatbot demo skips:
an explicit timeout, a retry policy that knows which errors are worth
retrying, structured cost logging, and a kill switch.
"""
import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

from openai import OpenAI

from ..config import diagnostic_log_content
from .errors import LLMNotConfigured, classify

logger = logging.getLogger("llm.cost")
retry_logger = logging.getLogger("llm.retry")

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "extract-v2.md"
PROMPT_VERSION = "extract-v2"
QUARANTINE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "quarantine.jsonl"

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

# We disable the SDK's own silent retries and drive retries ourselves, so the
# policy below (which errors, how many times, what backoff) is the only one
# in effect — see README for why.
_client: Optional[OpenAI] = None

# Indirection so tests can replace the wait without patching time.sleep globally.
_sleep = time.sleep


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LLMNotConfigured(f"{name} is not set")
    return value


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=_require_env("LLM_BASE_URL"),
            api_key=_require_env("LLM_API_KEY"),
            timeout=30.0,  # SDK default is 10 minutes — not a real timeout for an HTTP endpoint.
            max_retries=0,  # we drive retries ourselves below, deliberately.
        )
    return _client


def load_prompt() -> str:
    return PROMPT_PATH.read_text()


MAX_ATTEMPTS = 3  # 1 initial + 2 retries on transient errors
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 8.0


def backoff_seconds(attempt: int) -> float:
    """attempt is 0-based. 1s, 2s, 4s ... capped, plus up to 0.5s jitter."""
    return min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_MAX_SECONDS) + random.uniform(0, 0.5)


def _log_cost(model: str, usage, duration_ms: float, repaired: bool) -> None:
    line = {
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "duration_ms": round(duration_ms),
        "repaired": repaired,
    }
    logger.info(json.dumps(line))


def _content_of(res) -> str:
    """Some providers return no choices or a null content (e.g. on a refusal).
    Treat that as empty output so it flows into the normal parse -> repair
    path instead of crashing with an AttributeError/IndexError."""
    try:
        content = res.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return content or ""


def call_model(system_prompt: str, user_text: str, repair: bool = False) -> tuple[str, object, str]:
    """Calls the model with retry/backoff on transient errors only.

    Returns (raw_text, usage, model_name). Raises on exhausted retries or a
    non-retryable error (400/401/403, missing config) — those fail fast, on
    purpose. What counts as transient is defined once, in errors.classify.
    """
    client = _get_client()
    model = _require_env("LLM_MODEL")

    for attempt in range(MAX_ATTEMPTS):
        start = time.monotonic()
        try:
            res = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
            )
        except Exception as exc:  # noqa: BLE001 — classified below, re-raised if not transient
            info = classify(exc)
            # Log the category and status code only: provider error bodies
            # can echo request content, and the exception never holds the key.
            retry_logger.warning(json.dumps({
                "event": "llm_call_failed",
                "attempt": attempt + 1,
                "max_attempts": MAX_ATTEMPTS,
                "category": info.category.value,
                "upstream_status": info.upstream_status,
                "retryable": info.retryable,
            }))
            if not info.retryable or attempt == MAX_ATTEMPTS - 1:
                raise
            _sleep(backoff_seconds(attempt))
            continue

        duration_ms = (time.monotonic() - start) * 1000
        _log_cost(model, res.usage, duration_ms, repair)
        return _content_of(res), res.usage, model

    raise AssertionError("unreachable")  # pragma: no cover


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def quarantine(input_text: str, raw_output: str, error: str) -> None:
    """Record an output that failed validation even after repair.

    By default only metadata is written — lengths and short SHA-256
    fingerprints let you correlate and de-duplicate failures without storing
    receipt contents. Set DIAGNOSTIC_LOG_CONTENT=true while debugging a
    prompt to also capture the input text and raw model output.
    """
    record = {
        "at": time.time(),
        "prompt_version": PROMPT_VERSION,
        "error": error,
        "input_chars": len(input_text),
        "input_sha256": _fingerprint(input_text),
        "output_chars": len(raw_output or ""),
        "output_sha256": _fingerprint(raw_output or ""),
    }
    if diagnostic_log_content():
        record["input"] = input_text
        record["raw_output"] = raw_output
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUARANTINE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
