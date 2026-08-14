"""The worker side of the queue/worker pattern.

A fixed pool of daemon threads pulls job IDs off a queue.Queue and runs the
slow AI call outside the request/response cycle. Two non-negotiables live
here: a job can be retried a bounded number of times, and a job that never
succeeds gets logged somewhere a human will actually see it.
"""
import json
import logging
import queue
import threading
import time
from pathlib import Path

from .store import store

logger = logging.getLogger("jobs.worker")
alert_logger = logging.getLogger("jobs.alerts")

ALERTS_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "alerts.jsonl"

_work_queue: "queue.Queue[str]" = queue.Queue()
MAX_JOB_ATTEMPTS = 3  # 1 initial + 2 retries at the job level
_started = False
_start_lock = threading.Lock()


def enqueue(job_id: str) -> None:
    _work_queue.put(job_id)


def _raise_alert(job_id: str, attempts: int, error: str, input_preview: str) -> None:
    # A log line scrolls off a terminal. This file is the thing an on-call
    # human — or a real alerting pipeline tailing it — would actually see.
    alert_logger.error(
        '{"job_id": "%s", "attempts": %d, "final_error": "%s"}', job_id, attempts, error
    )
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_PATH, "a") as f:
        f.write(
            json.dumps(
                {
                    "job_id": job_id,
                    "attempts": attempts,
                    "final_error": error,
                    "input_preview": input_preview,
                    "at": time.time(),
                }
            )
            + "\n"
        )


def _process(job_id: str) -> None:
    from ..llm.parse import run_extraction  # deferred: keeps worker import-safe under stub/kill-switch

    job = store.get(job_id)
    if job is None:
        return

    for attempt in range(1, MAX_JOB_ATTEMPTS + 1):
        store.update(job_id, status="running", attempts=attempt)
        try:
            result = run_extraction(job.input_text)
            store.update(job_id, status="succeeded", result=result.model_dump(), error=None)
            logger.info(
                '{"job_id": "%s", "attempts": %d, "status": "succeeded"}', job_id, attempt
            )
            return
        except Exception as exc:  # noqa: BLE001 — a job must never crash the worker thread
            logger.warning(
                '{"job_id": "%s", "attempt": %d, "error": "%s"}', job_id, attempt, exc
            )
            if attempt < MAX_JOB_ATTEMPTS:
                time.sleep(2 ** attempt)  # backoff between job-level retries
                continue

            # Retries exhausted: quarantine the job, and make sure a human finds out.
            store.update(job_id, status="failed", error=str(exc))
            _raise_alert(job_id, attempt, str(exc), job.input_text[:80].replace('"', "'"))


def _worker_loop() -> None:
    while True:
        job_id = _work_queue.get()
        try:
            _process(job_id)
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