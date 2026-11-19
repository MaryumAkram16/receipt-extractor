"""LLM provider failures: which errors are retried, how often, and what callers see."""
import logging

import pytest

from app.llm import client as llm_client
from app.llm.errors import ErrorCategory, InvalidModelOutput, LLMNotConfigured, classify

from .conftest import FAKE_KEY, REAL_GET_CLIENT, VALID_OUTPUT, connection_error, status_error, timeout_error

RECEIPT = "Cafe Aroma\nTotal: PKR 1082"


@pytest.mark.parametrize("exc_factory, category, retryable", [
    (timeout_error, ErrorCategory.TIMEOUT, True),
    (connection_error, ErrorCategory.CONNECTION, True),
    (lambda: status_error(429), ErrorCategory.RATE_LIMITED, True),
    (lambda: status_error(500), ErrorCategory.PROVIDER_UNAVAILABLE, True),
    (lambda: status_error(502), ErrorCategory.PROVIDER_UNAVAILABLE, True),
    (lambda: status_error(503), ErrorCategory.PROVIDER_UNAVAILABLE, True),
    (lambda: status_error(504), ErrorCategory.PROVIDER_UNAVAILABLE, True),
    (lambda: status_error(501), ErrorCategory.PROVIDER_UNAVAILABLE, False),
    (lambda: status_error(400), ErrorCategory.BAD_REQUEST, False),
    (lambda: status_error(401), ErrorCategory.AUTH, False),
    (lambda: status_error(403), ErrorCategory.FORBIDDEN, False),
    (lambda: status_error(404), ErrorCategory.BAD_REQUEST, False),
    (lambda: InvalidModelOutput("x"), ErrorCategory.INVALID_OUTPUT, False),
    (lambda: LLMNotConfigured("x"), ErrorCategory.NOT_CONFIGURED, False),
    (lambda: RuntimeError("bug"), ErrorCategory.INTERNAL, False),
])
def test_error_classification(exc_factory, category, retryable):
    info = classify(exc_factory())
    assert info.category is category
    assert info.retryable is retryable


@pytest.mark.parametrize("transient", [
    timeout_error, connection_error,
    lambda: status_error(429), lambda: status_error(500), lambda: status_error(502),
    lambda: status_error(503), lambda: status_error(504),
])
def test_transient_error_is_retried_then_succeeds(fake_llm, sleeps, transient):
    fake_llm.script = [transient(), VALID_OUTPUT]
    raw, _, _ = llm_client.call_model("sys", RECEIPT)
    assert raw == VALID_OUTPUT
    assert len(fake_llm.calls) == 2
    assert len(sleeps["client"]) == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_permanent_error_fails_fast_without_retry(fake_llm, sleeps, code):
    fake_llm.script = [status_error(code)]
    with pytest.raises(Exception) as exc_info:
        llm_client.call_model("sys", RECEIPT)
    assert exc_info.value.status_code == code
    assert len(fake_llm.calls) == 1
    assert sleeps["client"] == []


def test_retry_exhaustion_raises_last_error_with_bounded_backoff(fake_llm, sleeps):
    fake_llm.script = [status_error(503)] * llm_client.MAX_ATTEMPTS
    with pytest.raises(Exception) as exc_info:
        llm_client.call_model("sys", RECEIPT)
    assert exc_info.value.status_code == 503
    assert len(fake_llm.calls) == llm_client.MAX_ATTEMPTS == 3
    # exponential: ~1s then ~2s (each plus <=0.5s jitter), no sleep after the last attempt
    assert len(sleeps["client"]) == 2
    assert 1.0 <= sleeps["client"][0] <= 1.5
    assert 2.0 <= sleeps["client"][1] <= 2.5


def test_backoff_is_capped():
    assert llm_client.backoff_seconds(10) <= llm_client.BACKOFF_MAX_SECONDS + 0.5


@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"])
def test_missing_provider_config_raises_typed_error(monkeypatch, missing):
    monkeypatch.setattr(llm_client, "_client", None)
    monkeypatch.setattr(llm_client, "_get_client", REAL_GET_CLIENT)
    monkeypatch.delenv(missing)
    with pytest.raises(LLMNotConfigured, match=missing):
        llm_client.call_model("sys", RECEIPT)


def test_missing_provider_config_is_503_on_endpoint(client, monkeypatch):
    monkeypatch.setattr(llm_client, "_client", None)
    monkeypatch.setattr(llm_client, "_get_client", REAL_GET_CLIENT)
    monkeypatch.delenv("LLM_API_KEY")
    resp = client.post("/extract", json={"text": RECEIPT})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "model provider is not configured"}


@pytest.mark.parametrize("script, status, detail", [
    ([timeout_error()] * 3, 504, "model call timed out"),
    ([connection_error()] * 3, 502, "could not reach the model provider"),
    ([status_error(429)] * 3, 503, "model provider is rate limiting requests; retry later"),
    ([status_error(500)] * 3, 502, "model provider is unavailable"),
    ([status_error(502)] * 3, 502, "model provider is unavailable"),
    ([status_error(503)] * 3, 502, "model provider is unavailable"),
    ([status_error(504)] * 3, 502, "model provider is unavailable"),
    ([status_error(401)], 502, "model provider rejected the request"),
    ([status_error(403)], 502, "model provider rejected the request"),
    ([status_error(400)], 502, "model provider rejected the request"),
])
def test_extract_endpoint_maps_provider_failures(client, fake_llm, script, status, detail):
    fake_llm.script = list(script)
    resp = client.post("/extract", json={"text": RECEIPT})
    assert resp.status_code == status
    assert resp.json() == {"detail": detail}
    assert fake_llm.script == [], "every scripted attempt should have been used, and no more"


def test_unexpected_error_is_a_bare_500(client, fake_llm, monkeypatch):
    import app.llm.parse as parse

    def boom(_text):
        raise RuntimeError("internal detail: /srv/secret/path")

    monkeypatch.setattr(parse, "extract", boom)
    resp = client.post("/extract", json={"text": RECEIPT})
    assert resp.status_code == 500
    assert "secret" not in resp.text


def test_failure_logs_never_contain_api_key_or_receipt(fake_llm, caplog):
    caplog.set_level(logging.DEBUG)
    fake_llm.script = [status_error(503)] * 3
    with pytest.raises(Exception):
        llm_client.call_model("sys", RECEIPT)
    assert FAKE_KEY not in caplog.text
    assert "Cafe Aroma" not in caplog.text
    assert '"category": "provider_unavailable"' in caplog.text
