"""One place that decides what a failure means.

Both retry layers (the HTTP-level loop in `client.call_model` and the
job-level loop in `jobs.worker`) and the public API error mapping use
`classify()`, so they can never disagree about whether an error is worth
retrying or what a caller is allowed to see about it.

Retry policy:
    transient (retry):     timeouts, connection errors, HTTP 429/500/502/503/504,
                           SQLite "database is locked"-style operational errors
    permanent (fail fast): HTTP 400/401/403 and every other 4xx, a missing
                           provider configuration, model output that still
                           fails schema validation after the one repair
                           attempt, and anything unrecognised
"""
import sqlite3
from dataclasses import dataclass
from enum import Enum

from openai import APIConnectionError, APIStatusError, APITimeoutError

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ErrorCategory(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection_error"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    AUTH = "provider_auth_failed"
    FORBIDDEN = "provider_forbidden"
    BAD_REQUEST = "provider_rejected_request"
    INVALID_OUTPUT = "invalid_model_output"
    NOT_CONFIGURED = "provider_not_configured"
    STORAGE = "storage_error"
    INTERNAL = "internal_error"


class InvalidModelOutput(ValueError):
    """Model output failed parsing/validation even after the repair attempt.

    Subclasses ValueError so existing `except ValueError` callers still work.
    The message is deliberately generic — the details (which can contain
    fragments of the model's output) go to the quarantine log, not here.
    """


class LLMNotConfigured(RuntimeError):
    """LLM_BASE_URL / LLM_API_KEY / LLM_MODEL is missing."""


@dataclass(frozen=True)
class ErrorInfo:
    category: ErrorCategory
    retryable: bool
    http_status: int      # status the sync /extract endpoint answers with
    public_message: str   # safe to show a caller: no provider bodies, no inputs
    upstream_status: int | None = None  # provider HTTP status, for server-side logs only


_PUBLIC = {
    ErrorCategory.TIMEOUT: (504, "model call timed out"),
    ErrorCategory.CONNECTION: (502, "could not reach the model provider"),
    ErrorCategory.RATE_LIMITED: (503, "model provider is rate limiting requests; retry later"),
    ErrorCategory.PROVIDER_UNAVAILABLE: (502, "model provider is unavailable"),
    ErrorCategory.AUTH: (502, "model provider rejected the request"),
    ErrorCategory.FORBIDDEN: (502, "model provider rejected the request"),
    ErrorCategory.BAD_REQUEST: (502, "model provider rejected the request"),
    ErrorCategory.INVALID_OUTPUT: (422, "model output failed validation after repair"),
    ErrorCategory.NOT_CONFIGURED: (503, "model provider is not configured"),
    ErrorCategory.STORAGE: (503, "storage temporarily unavailable"),
    ErrorCategory.INTERNAL: (500, "internal error"),
}

_RETRYABLE = {
    ErrorCategory.TIMEOUT,
    ErrorCategory.CONNECTION,
    ErrorCategory.RATE_LIMITED,
    ErrorCategory.PROVIDER_UNAVAILABLE,
    ErrorCategory.STORAGE,
}


def _info(category: ErrorCategory, upstream_status: int | None = None) -> ErrorInfo:
    http_status, message = _PUBLIC[category]
    return ErrorInfo(category, category in _RETRYABLE, http_status, message, upstream_status)


def classify(exc: BaseException) -> ErrorInfo:
    # Order matters: APITimeoutError is a subclass of APIConnectionError.
    if isinstance(exc, APITimeoutError):
        return _info(ErrorCategory.TIMEOUT)
    if isinstance(exc, APIConnectionError):
        return _info(ErrorCategory.CONNECTION)
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if status == 429:
            category = ErrorCategory.RATE_LIMITED
        elif status in RETRYABLE_STATUS:
            category = ErrorCategory.PROVIDER_UNAVAILABLE
        elif status == 401:
            category = ErrorCategory.AUTH
        elif status == 403:
            category = ErrorCategory.FORBIDDEN
        elif 500 <= status < 600:
            # 501, 505, ... — not in the known-transient set; don't burn quota on them.
            category = ErrorCategory.PROVIDER_UNAVAILABLE
            return ErrorInfo(category, False, *_PUBLIC[category], upstream_status=status)
        else:
            category = ErrorCategory.BAD_REQUEST
        return _info(category, status)
    if isinstance(exc, InvalidModelOutput):
        return _info(ErrorCategory.INVALID_OUTPUT)
    if isinstance(exc, LLMNotConfigured):
        return _info(ErrorCategory.NOT_CONFIGURED)
    if isinstance(exc, sqlite3.OperationalError):
        return _info(ErrorCategory.STORAGE)
    return _info(ErrorCategory.INTERNAL)


def is_retryable(exc: BaseException) -> bool:
    return classify(exc).retryable
