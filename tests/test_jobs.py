"""Background jobs: lifecycle, idempotency, duplicate protection, retry policy."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.jobs import worker
from app.jobs.store import IdempotencyConflict, JobStore, request_fingerprint, store
from app.scheduler import trigger_scheduled_report

from .conftest import (
    FAKE_KEY, VALID_OUTPUT, queued_job_ids, run_queued_jobs, status_error, timeout_error,
)

RECEIPT = "Cafe Aroma\n12 Jun 2025\nTotal: PKR 1082"


def _alerts():
    if not worker.ALERTS_PATH.exists():
        return []
    return [json.loads(line) for line in worker.ALERTS_PATH.read_text().splitlines()]


# --- lifecycle --------------------------------------------------------------

def test_extract_job_succeeds(client, fake_llm):
    fake_llm.script = [VALID_OUTPUT]
    resp = client.post("/jobs/extract", json={"text": RECEIPT})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert resp.headers["Location"] == f"/jobs/{body['job_id']}"

    pending = client.get(f"/jobs/{body['job_id']}").json()
    assert pending["status"] == "pending" and pending["attempts"] == 0

    run_queued_jobs()
    done = client.get(f"/jobs/{body['job_id']}").json()
    assert done == {
        "job_id": body["job_id"], "kind": "extract", "status": "succeeded", "attempts": 1,
        "result": {"vendor": "Cafe Aroma", "date": "2025-06-12", "total_amount": 1082.0,
                   "currency": "PKR", "confidence": 0.95, "needs_review": False},
        "error": None, "error_category": None,
    }


def test_status_transitions_pending_running_succeeded(fake_llm, monkeypatch):
    fake_llm.script = [VALID_OUTPUT]
    job, _ = store.create_or_get("extract", {"text": RECEIPT}, None)
    seen = [store.get(job.id).status]

    real_run = worker._run_task

    def spy(j):
        seen.append(store.get(j.id).status)
        return real_run(j)

    monkeypatch.setattr(worker, "_run_task", spy)
    worker._process(job.id)
    seen.append(store.get(job.id).status)
    assert seen == ["pending", "running", "succeeded"]


def test_failed_job_reports_sanitized_error(client, fake_llm):
    fake_llm.script = ["bad", '{"confidence": "LEAKED-VENDOR", "needs_review": true}']
    job_id = client.post("/jobs/extract", json={"text": RECEIPT}).json()["job_id"]
    run_queued_jobs()
    status = client.get(f"/jobs/{job_id}").json()
    assert status["status"] == "failed"
    assert status["error_category"] == "invalid_model_output"
    assert status["error"] == "model output failed validation after repair"
    assert "LEAKED" not in json.dumps(status)
    # invalid output is permanent: no job-level retry, so exactly 2 model calls (initial + repair)
    assert status["attempts"] == 1
    assert len(fake_llm.calls) == 2


def test_unknown_job_id_is_404(client):
    resp = client.get("/jobs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no job with that id"}


def test_processing_unknown_job_id_is_a_noop():
    worker._process("does-not-exist")  # must not raise


def test_unknown_job_kind_fails_job_not_worker():
    job, _ = store.create_or_get("mystery", {}, None)
    worker._process(job.id)
    assert store.get(job.id).status == "failed"
    assert store.get(job.id).error_category == "internal_error"


# --- idempotency --------------------------------------------------------------

def test_same_key_same_payload_returns_original_job_and_enqueues_once(client):
    body = {"text": RECEIPT, "idempotency_key": "order-123"}
    first = client.post("/jobs/extract", json=body)
    second = client.post("/jobs/extract", json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert queued_job_ids() == [first.json()["job_id"]]


def test_same_key_different_payload_is_409(client):
    first = client.post("/jobs/extract", json={"text": RECEIPT, "idempotency_key": "order-123"})
    second = client.post("/jobs/extract", json={"text": "other receipt", "idempotency_key": "order-123"})
    assert first.status_code == 202
    assert second.status_code == 409
    assert "different request payload" in second.json()["detail"]
    assert queued_job_ids() == [first.json()["job_id"]]


def test_no_key_always_creates_new_jobs(client):
    a = client.post("/jobs/extract", json={"text": RECEIPT}).json()["job_id"]
    b = client.post("/jobs/extract", json={"text": RECEIPT}).json()["job_id"]
    assert a != b
    assert queued_job_ids() == [a, b]


def test_empty_key_behaves_like_no_key(client):
    a = client.post("/jobs/extract", json={"text": RECEIPT, "idempotency_key": ""}).json()["job_id"]
    b = client.post("/jobs/extract", json={"text": "other", "idempotency_key": ""}).json()["job_id"]
    assert a != b


def test_keys_are_scoped_per_job_kind(client):
    extract = client.post("/jobs/extract", json={"text": RECEIPT, "idempotency_key": "shared"})
    report = client.post("/jobs/report", json={"idempotency_key": "shared"})
    assert extract.status_code == report.status_code == 202
    assert extract.json()["job_id"] != report.json()["job_id"]
    assert client.get(f"/jobs/{report.json()['job_id']}").json()["kind"] == "report"


def test_report_payload_is_normalized_before_fingerprinting(client):
    a = client.post("/jobs/report", json={"start_date": "2025-06-01", "idempotency_key": "r1"})
    b = client.post("/jobs/report", json={"start_date": "2025-06-01T00:00:00+00:00", "idempotency_key": "r1"})
    c = client.post("/jobs/report", json={"start_date": "2025-06-02", "idempotency_key": "r1"})
    assert a.json()["job_id"] == b.json()["job_id"]
    assert c.status_code == 409


def test_fingerprint_is_canonical_and_kind_scoped():
    assert request_fingerprint("report", {"a": 1, "b": 2}) == request_fingerprint("report", {"b": 2, "a": 1})
    assert request_fingerprint("report", {"a": 1}) != request_fingerprint("extract", {"a": 1})


def test_replay_after_completion_returns_finished_job_without_rerun(client, fake_llm):
    fake_llm.script = [VALID_OUTPUT]
    body = {"text": RECEIPT, "idempotency_key": "k1"}
    job_id = client.post("/jobs/extract", json=body).json()["job_id"]
    run_queued_jobs()
    replay = client.post("/jobs/extract", json=body)
    assert replay.json() == {"job_id": job_id, "status": "succeeded"}
    assert queued_job_ids() == []
    assert len(fake_llm.calls) == 1


def test_concurrent_requests_with_same_key_create_one_job():
    s = JobStore()
    n = 32
    barrier = threading.Barrier(n)

    def submit(_):
        barrier.wait()
        return s.create_or_get("extract", {"text": RECEIPT}, "same-key")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(submit, range(n)))

    assert len({job.id for job, _ in results}) == 1
    assert sum(created for _, created in results) == 1


def test_concurrent_conflicting_payloads_get_one_job_and_conflicts():
    s = JobStore()
    n = 16
    barrier = threading.Barrier(n)

    def submit(i):
        barrier.wait()
        try:
            return s.create_or_get("extract", {"text": f"payload-{i % 2}"}, "k")
        except IdempotencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(submit, range(n)))

    jobs = [r for r in results if r != "conflict"]
    assert len({job.id for job, _ in jobs}) == 1
    assert results.count("conflict") == n // 2


def test_duplicate_enqueue_runs_job_only_once(fake_llm):
    fake_llm.script = [VALID_OUTPUT]  # a second run would exhaust the script and fail
    job, _ = store.create_or_get("extract", {"text": RECEIPT}, None)
    worker.enqueue(job.id)
    worker.enqueue(job.id)
    run_queued_jobs()
    assert store.get(job.id).status == "succeeded"
    assert store.get(job.id).attempts == 1
    assert len(fake_llm.calls) == 1


def test_claim_is_exclusive_under_concurrency():
    job, _ = store.create_or_get("extract", {"text": RECEIPT}, None)
    n = 16
    barrier = threading.Barrier(n)

    def claim(_):
        barrier.wait()
        return store.claim(job.id)

    with ThreadPoolExecutor(max_workers=n) as pool:
        claims = [c for c in pool.map(claim, range(n)) if c is not None]
    assert len(claims) == 1


# --- worker retry policy ------------------------------------------------------

def test_transient_failure_is_retried_at_job_level(fake_llm, sleeps):
    # attempt 1: client exhausts its 3 HTTP attempts on 503; attempt 2 succeeds first try
    fake_llm.script = [status_error(503)] * 3 + [VALID_OUTPUT]
    job, _ = store.create_or_get("extract", {"text": RECEIPT}, None)
    worker._process(job.id)
    final = store.get(job.id)
    assert final.status == "succeeded"
    assert final.attempts == 2
    assert sleeps["worker"] == [2.0]


@pytest.mark.parametrize("code", [400, 401, 403])
def test_permanent_failure_is_not_retried_at_job_level(fake_llm, sleeps, code):
    fake_llm.script = [status_error(code)]
    job, _ = store.create_or_get("extract", {"text": RECEIPT}, None)
    worker._process(job.id)
    final = store.get(job.id)
    assert final.status == "failed"
    assert final.attempts == 1
    assert len(fake_llm.calls) == 1
    assert sleeps["worker"] == [] and sleeps["client"] == []


def test_nested_retries_are_bounded(fake_llm, sleeps):
    """A provider that stays down costs at most JOB_MAX_ATTEMPTS x client MAX_ATTEMPTS requests."""
    fake_llm.script = [timeout_error()] * 6
    job, _ = store.create_or_get("extract", {"text": RECEIPT}, None)
    worker._process(job.id)
    final = store.get(job.id)
    assert final.status == "failed"
    assert final.error_category == "timeout"
    assert final.attempts == 2
    assert len(fake_llm.calls) == 6
    assert fake_llm.script == []


def test_job_max_attempts_is_configurable_and_clamped(monkeypatch):
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "1")
    assert worker.max_job_attempts() == 1
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "99")
    assert worker.max_job_attempts() == 5
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "nonsense")
    assert worker.max_job_attempts() == worker.DEFAULT_MAX_JOB_ATTEMPTS


def test_job_backoff_is_exponential_and_capped():
    assert [worker.job_backoff_seconds(a) for a in (1, 2, 3)] == [2.0, 4.0, 8.0]
    assert worker.job_backoff_seconds(20) == worker.JOB_BACKOFF_MAX_SECONDS


# --- alerting / privacy -----------------------------------------------------

def test_exhausted_job_writes_alert_without_receipt_text(fake_llm, caplog):
    fake_llm.script = [status_error(401)]
    job, _ = store.create_or_get("extract", {"text": RECEIPT}, None)
    worker._process(job.id)
    [alert] = _alerts()
    assert alert["job_id"] == job.id
    assert alert["kind"] == "extract"
    assert alert["error_category"] == "provider_auth_failed"
    assert alert["final_error"] == "model provider rejected the request"
    assert alert["input_chars"] == len(RECEIPT)
    assert "input_preview" not in alert
    dumped = json.dumps(alert) + caplog.text
    assert "Cafe Aroma" not in dumped
    assert FAKE_KEY not in dumped


def test_alert_line_is_valid_json_even_with_quotes_in_errors(monkeypatch):
    job, _ = store.create_or_get("extract", {"text": 'he said "hi"'}, None)

    def boom(_job):
        raise RuntimeError('error with "quotes" and \\ backslash')

    monkeypatch.setattr(worker, "_run_task", boom)
    worker._process(job.id)
    [alert] = _alerts()  # json.loads succeeded
    assert alert["error_category"] == "internal_error"


# --- reports via jobs + scheduler ------------------------------------------

def test_report_job_succeeds_and_links_pdf(client):
    job_id = client.post("/jobs/report", json={}).json()["job_id"]
    run_queued_jobs()
    status = client.get(f"/jobs/{job_id}").json()
    assert status["status"] == "succeeded"
    assert status["kind"] == "report"
    assert status["result"]["report_url"].startswith("/reports/spending-report-")
    assert status["result"]["summary"]["total_records"] == 0


def test_scheduler_tick_enqueues_report_job():
    job_id = trigger_scheduled_report()
    assert queued_job_ids() == [job_id]
    run_queued_jobs()
    assert store.get(job_id).status == "succeeded"


def test_scheduler_disabled_by_default(monkeypatch):
    import app.scheduler as scheduler

    started = []
    monkeypatch.setattr(scheduler.threading, "Thread", lambda *a, **k: started.append(1))
    monkeypatch.setattr(scheduler, "_started", False)
    monkeypatch.delenv("REPORT_SCHEDULE_ENABLED", raising=False)
    scheduler.start_scheduler()
    assert started == []
