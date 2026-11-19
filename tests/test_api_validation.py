"""Request validation: bad input is rejected with 400 before any model call."""
import pytest

from .conftest import VALID_OUTPUT

EXTRACT_ENDPOINTS = ["/extract", "/jobs/extract"]


def test_health_reports_status_and_llm_mode(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok", "llm_mode": "live", "prompt_version": "extract-v2", "model": "test-model",
    }


@pytest.mark.parametrize("env", [{"LLM_STUB": "1"}, {"LLM_ENABLED": "false"}])
def test_health_hides_model_when_not_live(client, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    body = client.get("/health").json()
    assert body["model"] is None
    assert body["prompt_version"] == "extract-v2"


def test_health_never_exposes_api_key(client):
    from .conftest import FAKE_KEY
    assert FAKE_KEY not in client.get("/health").text


@pytest.mark.parametrize("env, expected", [
    ({"LLM_STUB": "1"}, "stub"),
    ({"LLM_STUB": "true"}, "stub"),
    ({"LLM_ENABLED": "false"}, "disabled"),
    ({"LLM_ENABLED": "0", "LLM_STUB": "1"}, "disabled"),  # kill switch wins
])
def test_health_llm_mode_reflects_flags(client, monkeypatch, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert client.get("/health").json()["llm_mode"] == expected


def test_valid_receipt_text_returns_200(client, fake_llm):
    fake_llm.script = [VALID_OUTPUT]
    resp = client.post("/extract", json={"text": "Cafe Aroma\n12 Jun 2025\nTotal: PKR 1082"})
    assert resp.status_code == 200
    assert resp.json() == {
        "vendor": "Cafe Aroma", "date": "2025-06-12", "total_amount": 1082.0,
        "currency": "PKR", "confidence": 0.95, "needs_review": False,
    }


@pytest.mark.parametrize("endpoint", EXTRACT_ENDPOINTS)
@pytest.mark.parametrize("body, field", [
    ({}, "text"),                       # missing required field
    ({"text": ""}, "text"),             # empty
    ({"text": "   \n\t "}, "text"),     # whitespace only
    ({"text": 123}, "text"),            # wrong type
    ({"text": None}, "text"),
    ({"text": ["a"]}, "text"),
    ({"text": "x" * 4001}, "text"),     # over the max length
])
def test_invalid_text_is_rejected_with_400(client, endpoint, body, field):
    # no fake_llm fixture: any model call here would fail the test
    resp = client.post(endpoint, json=body)
    assert resp.status_code == 400
    assert resp.json()["error"].startswith(f"{field}: ")


@pytest.mark.parametrize("endpoint", EXTRACT_ENDPOINTS)
def test_max_length_text_is_accepted(client, monkeypatch, endpoint):
    monkeypatch.setenv("LLM_STUB", "1")
    resp = client.post(endpoint, json={"text": "x" * 4000})
    assert resp.status_code in (200, 202)


@pytest.mark.parametrize("raw_body", ["[1, 2]", "\"just a string\"", "not json", ""])
def test_non_object_or_malformed_body_is_400(client, raw_body):
    resp = client.post("/extract", content=raw_body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert not error.startswith(":"), "field name must never be empty"


def test_validation_error_does_not_echo_input(client):
    secret = "CARD 4111-1111-1111-1111 " + "x" * 4000
    resp = client.post("/extract", json={"text": secret})
    assert resp.status_code == 400
    assert "4111" not in resp.text


def test_blank_text_message_is_readable(client):
    resp = client.post("/extract", json={"text": "   "})
    assert resp.json() == {"error": "text: text must not be blank"}


@pytest.mark.parametrize("body", [
    {"start_date": "not-a-date"},
    {"end_date": "2025-13-01"},
    {"start_date": "2025-06-10", "end_date": "2025-06-01"},
    {"start_date": 20250101},
])
def test_report_job_rejects_bad_dates(client, body):
    resp = client.post("/jobs/report", json=body)
    assert resp.status_code == 400


def test_idempotency_key_length_is_bounded(client):
    resp = client.post("/jobs/extract", json={"text": "receipt", "idempotency_key": "k" * 256})
    assert resp.status_code == 400
    assert resp.json()["error"].startswith("idempotency_key: ")
