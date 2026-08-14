"""Thin wrapper around the OpenAI-compatible client.

Owns the four things a real LLM call needs that a chatbot demo skips:
an explicit timeout, a retry policy that knows which errors are worth
retrying, structured cost logging, and a kill switch.
"""
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

from openai import APIStatusError, APITimeoutError, OpenAI

logger = logging.getLogger("llm.cost")

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "extract-v2.md"
PROMPT_VERSION = "extract-v2"
QUARANTINE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "quarantine.jsonl"

# We disable the SDK's own silent retries and drive retries ourselves, so the
# policy below (which errors, how many times, what backoff) is the only one
# in effect — see README for why.
_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ["LLM_API_KEY"],
            timeout=30.0,  # SDK default is 10 minutes — not a real timeout for an HTTP endpoint.
            max_retries=0,  # we drive retries ourselves below, deliberately.
        )
    return _client


def load_prompt() -> str:
    return PROMPT_PATH.read_text()


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3  # 1 initial + 2 retries on transient errors


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


def call_model(system_prompt: str, user_text: str, repair: bool = False) -> tuple[str, object, str]:
    """Calls the model with retry/backoff on transient errors only.

    Returns (raw_text, usage, model_name). Raises on exhausted retries or a
    non-retryable error (400/401/403) — those fail fast, on purpose.
    """
    client = _get_client()
    model = os.environ["LLM_MODEL"]
    last_exc = None

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
            duration_ms = (time.monotonic() - start) * 1000
            _log_cost(model, res.usage, duration_ms, repair)
            return res.choices[0].message.content, res.usage, model
        except APITimeoutError as exc:
            last_exc = exc
        except APIStatusError as exc:
            if exc.status_code not in RETRYABLE_STATUS:
                # 400/401/403 etc: a bad key or bad request will still be bad
                # in four seconds. Fail fast, don't burn quota retrying it.
                raise
            last_exc = exc

        if attempt < MAX_ATTEMPTS - 1:
            backoff = (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(backoff)

    raise last_exc


def quarantine(input_text: str, raw_output: str, error: str) -> None:
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUARANTINE_PATH, "a") as f:
        f.write(
            json.dumps(
                {
                    "prompt_version": PROMPT_VERSION,
                    "input": input_text,
                    "raw_output": raw_output,
                    "error": error,
                }
            )
            + "\n"
        )