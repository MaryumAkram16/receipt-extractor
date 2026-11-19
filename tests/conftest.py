"""Shared fixtures.

Every test runs with:
  * a temporary SQLite file, reports directory and logs directory;
  * a fake OpenAI-compatible client — no test can reach a real provider
    (an unscripted model call fails the test loudly);
  * no-op retry sleeps (the waits are recorded so tests can assert on them);
  * an empty job store and queue, and no background worker threads — jobs
    are processed synchronously with `run_queued_jobs()`, so nothing depends
    on timing.
"""
import os

# Set before the app (and python-dotenv) is imported so a developer's local
# .env can never switch the test run into stub or live mode.
os.environ.update({
    "LLM_STUB": "0",
    "LLM_ENABLED": "true",
    "LLM_BASE_URL": "http://llm.invalid/v1",
    "LLM_API_KEY": "sk-test-not-a-real-key-1234567890",
    "LLM_MODEL": "test-model",
    "REPORT_SCHEDULE_ENABLED": "false",
    "DIAGNOSTIC_LOG_CONTENT": "false",
    "JOB_MAX_ATTEMPTS": "2",
})

import json
import queue
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app import db
from app.jobs import worker
from app.jobs.store import store
from app.llm import client as llm_client
from app.main import app
from app.reports import generator

FAKE_KEY = os.environ["LLM_API_KEY"]
REAL_GET_CLIENT = llm_client._get_client  # captured before the no_real_llm guard replaces it
_REQUEST = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")

VALID_OUTPUT = json.dumps({
    "vendor": "Cafe Aroma", "date": "2025-06-12", "total_amount": 1082,
    "currency": "PKR", "confidence": 0.95, "needs_review": False,
})


def status_error(code: int) -> APIStatusError:
    return APIStatusError(f"Error code: {code}", response=httpx.Response(code, request=_REQUEST), body=None)


def timeout_error() -> APITimeoutError:
    return APITimeoutError(request=_REQUEST)


def connection_error() -> APIConnectionError:
    return APIConnectionError(request=_REQUEST)


class FakeLLM:
    """Stands in for the OpenAI client. `script` is consumed in order: a str
    is returned as the message content (None means null content), an
    exception instance is raised."""

    def __init__(self) -> None:
        self.script: list = []
        self.calls: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, temperature, messages):
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("unexpected model call: FakeLLM script exhausted")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_STUB", "0")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("DIAGNOSTIC_LOG_CONTENT", "false")
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "2")

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "extractions.db")
    monkeypatch.setattr(generator, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(llm_client, "QUARANTINE_PATH", tmp_path / "logs" / "quarantine.jsonl")
    monkeypatch.setattr(worker, "ALERTS_PATH", tmp_path / "logs" / "alerts.jsonl")
    db.init_db()

    store.clear()
    while True:
        try:
            worker._work_queue.get_nowait()
            worker._work_queue.task_done()
        except queue.Empty:
            break
    yield
    store.clear()


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm_client, "_get_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Default guard: any model call made without the fake_llm fixture fails."""
    def _refuse():
        raise AssertionError("test attempted a real LLM call; use the fake_llm fixture")
    monkeypatch.setattr(llm_client, "_get_client", _refuse)


@pytest.fixture
def sleeps(monkeypatch) -> dict[str, list[float]]:
    recorded = {"client": [], "worker": []}
    monkeypatch.setattr(llm_client, "_sleep", lambda s: recorded["client"].append(s))
    monkeypatch.setattr(worker, "_sleep", lambda s: recorded["worker"].append(s))
    return recorded


@pytest.fixture(autouse=True)
def _default_no_sleep(monkeypatch):
    monkeypatch.setattr(llm_client, "_sleep", lambda s: None)
    monkeypatch.setattr(worker, "_sleep", lambda s: None)


@pytest.fixture
def client() -> TestClient:
    # Deliberately not used as a context manager: the lifespan (which starts
    # background worker threads) does not run, so tests drive the worker
    # synchronously via run_queued_jobs().
    return TestClient(app, raise_server_exceptions=False)


def run_queued_jobs() -> list[str]:
    processed = []
    while True:
        try:
            job_id = worker._work_queue.get_nowait()
        except queue.Empty:
            return processed
        try:
            worker._process(job_id)
            processed.append(job_id)
        finally:
            worker._work_queue.task_done()


def queued_job_ids() -> list[str]:
    return list(worker._work_queue.queue)
