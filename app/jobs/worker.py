"""The worker side of the queue/worker pattern.

A fixed pool of daemon threads pulls job IDs off a queue.Queue and runs the
slow work outside the request/response cycle. Two non-negotiables live
here: a job can be retried a bounded number of times, and a job that never
succeeds gets logged somewhere a human will actually see it. The same
machinery drives both job kinds ("extract" and "report") — only the task
function each one calls differs.

Retry policy (shared with the LLM client via llm.errors.classify):
    - Only transient failures are retried at the job level (timeouts,
      connection errors, 429, 500/502/503/504, SQLite operational errors).
    - Permanent failures (400/401/403, missing provider config, output that
      failed validation after its one repair attempt, unknown errors) fail
      the job immediately — retrying a bad API key just burns quota.
    - The LLM client already makes up to 3 HTTP attempts per model call, so
      the job layer defaults to 2 attempts (JOB_MAX_ATTEMPTS). Worst case for
      a provider that stays down: 2 job attempts x 3 HTTP attempts = 6
      requests, instead of the 9+ that two independent 3x loops produce.
"""
import hashlib
import json
import logging
import queue
import threading
import time
from pathlib import Path

from ..config import env_int
from ..llm.errors import classify
from .store import FAILED, RUNNING, SUCCEEDED, Job, store

logger = logging.getLogger("jobs.worker")
alert_logger = logging.getLogger("jobs.alerts")

ALERTS_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "alerts.jsonl"

_work_queue: "queue.Queue[str]" = queue.Queue()
DEFAULT_MAX_JOB_ATTEMPTS = 2
JOB_BACKOFF_BASE_SECONDS = 2.0
JOB_BACKOFF_MAX_SECONDS = 30.0
_started = False
_start_lock = threading.Lock()

# Indirection so tests can skip the wait without patching time.sleep globally.
_sleep = time.sleep


def max_job_attempts() -> int:
    return env_int("JOB_MAX_ATTEMPTS", DEFAULT_MAX_JOB_ATTEMPTS, minimum=1, maximum=5)


def job_backoff_seconds(attempt: int) -> float:
    """attempt is 1-based: 2s after the first failure, 4s after the second, capped."""
    return min(JOB_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), JOB_BACKOFF_MAX_SECONDS)


def enqueue(job_id: str) -> None:
    _work_queue.put(job_id)


def _input_metadata(job: Job) -> dict:
    """Non-sensitive description of a job's input for diagnostics: sizes and
    a short fingerprint for extract jobs (never the receipt text itself),
    the date window for report jobs."""
    if job.kind == "extract":
        text = job.input_data.get("text", "")
        return {
            "input_chars": len(text),
            "input_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
        }
    return {
        "start_date": job.input_data.get("start_date"),
        "end_date": job.input_data.get("end_date"),
    }


def _raise_alert(job: Job, attempts: int, category: str, message: str) -> None:
    # A log line scrolls off a terminal. This file is the thing an on-call
    # human — or a real alerting pipeline tailing it — would actually see.
    record = {
        "at": time.time(),
        "job_id": job.id,
        "kind": job.kind,
        "attempts": attempts,
        "error_category": category,
        "final_error": message,
        **_input_metadata(job),
    }
    line = json.dumps(record)
    alert_logger.error(line)
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run_task(job: Job) -> dict:
    """Dispatches to the right task function for this job's kind. Each task
    function takes the job's input_data and returns a JSON-serializable dict
    — that's the only contract the worker cares about.
    """
    if job.kind == "extract":
        from ..llm.parse import run_extraction  # deferred: keeps worker import-safe under stub/kill-switch
        result = run_extraction(job.input_data["text"])
        return result.model_dump(mode="json")

    if job.kind == "report":
        from ..reports.generator import generate_report
        return generate_report(
            start_date=job.input_data.get("start_date"),
            end_date=job.input_data.get("end_date"),
        )

    raise ValueError(f"unknown job kind: {job.kind}")


def _process(job_id: str) -> None:
    job = store.claim(job_id)
    if job is None:
        # Unknown id, or already claimed/finished by another worker: a
        # duplicate enqueue must never run the work twice.
        logger.info(json.dumps({"event": "job_skipped", "job_id": job_id}))
        return

    max_attempts = max_job_attempts()
    for attempt in range(1, max_attempts + 1):
        store.update(job_id, status=RUNNING, attempts=attempt)
        try:
            result = _run_task(job)
        except Exception as exc:  # noqa: BLE001 — a job must never crash the worker thread
            info = classify(exc)
            will_retry = info.retryable and attempt < max_attempts
            logger.warning(
                json.dumps({
                    "event": "job_attempt_failed",
                    "job_id": job_id,
                    "kind": job.kind,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "error_category": info.category.value,
                    "error_type": type(exc).__name__,
                    "upstream_status": info.upstream_status,
                    "will_retry": will_retry,
                }),
                # Tracebacks only for genuinely unexpected errors (code bugs);
                # provider/validation failures are fully described above.
                exc_info=info.category.value == "internal_error",
            )
            if will_retry:
                _sleep(job_backoff_seconds(attempt))
                continue

            # Permanent failure or retries exhausted: fail the job, and make
            # sure a human finds out. Only the sanitized message is stored —
            # GET /jobs/{id} is public.
            store.update(job_id, status=FAILED, error=info.public_message,
                         error_category=info.category.value, result=None)
            _raise_alert(job, attempt, info.category.value, info.public_message)
            return

        store.update(job_id, status=SUCCEEDED, result=result, error=None, error_category=None)
        logger.info(json.dumps({
            "event": "job_succeeded", "job_id": job_id, "kind": job.kind, "attempts": attempt,
        }))
        return


def _worker_loop() -> None:
    while True:
        job_id = _work_queue.get()
        try:
            _process(job_id)
        except Exception:  # noqa: BLE001 — keep the thread alive no matter what
            logger.exception("worker loop error")
        finally:
            _work_queue.task_done()


def start_workers(count: int = 2) -> None:
    global _started
    with _start_lock:
        if _started:
            return
        for _ in range(count):
            t = threading.Thread(target=_worker_loop, daemon=True)
            t.start()
        _started = True
