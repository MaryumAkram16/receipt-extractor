"""The worker side of the queue/worker pattern.

A fixed pool of daemon threads pulls job IDs off a queue.Queue and runs the
slow work outside the request/response cycle. Two non-negotiables live
here: a job can be retried a bounded number of times, and a job that never
succeeds gets logged somewhere a human will actually see it. The same
machinery drives both job kinds ("extract" and "report") — only the task
function each one calls differs.
"""
import json
import logging
import queue
import threading
import time
from pathlib import Path

from .store import store, Job

logger = logging.getLogger("jobs.worker")
alert_logger = logging.getLogger("jobs.alerts")

ALERTS_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "alerts.jsonl"

_work_queue: "queue.Queue[str]" = queue.Queue()
MAX_JOB_ATTEMPTS = 3  # 1 initial + 2 retries at the job level
_started = False
_start_lock = threading.Lock()


def enqueue(job_id: str) -> None:
    _work_queue.put(job_id)


def _raise_alert(job_id: str, attempts: int, error: str, preview: str) -> None:
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
                    "input_preview": preview,
                    "at": time.time(),
                }
            )
            + "\n"
        )


def _run_task(job: Job) -> dict:
    """Dispatches to the right task function for this job's kind. Each task
    function takes the job's input_data and returns a JSON-serializable dict
    — that's the only contract the worker cares about.
    """
    if job.kind == "extract":
        from ..llm.parse import run_extraction  # deferred: keeps worker import-safe under stub/kill-switch
        result = run_extraction(job.input_data["text"])
        return result.model_dump()

    if job.kind == "report":
        from ..reports.generator import generate_report
        return generate_report(
            start_date=job.input_data.get("start_date"),
            end_date=job.input_data.get("end_date"),
        )

    raise ValueError(f"unknown job kind: {job.kind}")


def _preview(job: Job) -> str:
    if job.kind == "extract":
        return job.input_data.get("text", "")[:80].replace('"', "'")
    return f"report {job.input_data.get('start_date', 'all')}..{job.input_data.get('end_date', 'all')}"


def _process(job_id: str) -> None:
    job = store.get(job_id)
    if job is None:
        return

    for attempt in range(1, MAX_JOB_ATTEMPTS + 1):
        store.update(job_id, status="running", attempts=attempt)
        try:
            result = _run_task(job)
            store.update(job_id, status="succeeded", result=result, error=None)
            logger.info(
                '{"job_id": "%s", "kind": "%s", "attempts": %d, "status": "succeeded"}',
                job_id, job.kind, attempt,
            )
            return
        except Exception as exc:  # noqa: BLE001 — a job must never crash the worker thread
            logger.warning(
                '{"job_id": "%s", "kind": "%s", "attempt": %d, "error": "%s"}',
                job_id, job.kind, attempt, exc,
            )
            if attempt < MAX_JOB_ATTEMPTS:
                time.sleep(2 ** attempt)  # backoff between job-level retries
                continue

            # Retries exhausted: quarantine the job, and make sure a human finds out.
            store.update(job_id, status="failed", error=str(exc))
            _raise_alert(job_id, attempt, str(exc), _preview(job))


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