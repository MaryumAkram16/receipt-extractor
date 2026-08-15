"""Stretch goal: generate a report on a fixed schedule, not just on demand.

A daemon thread that sleeps for REPORT_SCHEDULE_INTERVAL_SECONDS and then
enqueues a report job through the exact same queue/worker path a manual
POST /jobs/report would use — scheduling doesn't get its own execution
logic, it just triggers the existing one on a timer.

This is deliberately not a real scheduler (no persistence across restarts,
no catch-up for missed runs, no cron expressions) — for a single-process
dev app that's the right amount of machinery. The first thing to reach for
instead once this needs to survive restarts or run across multiple
instances is something like APScheduler with a persistent job store, or an
external cron hitting POST /jobs/report.
"""
import logging
import os
import threading
import time

from .jobs.store import store
from .jobs.worker import enqueue

logger = logging.getLogger("reports.scheduler")

_started = False
_start_lock = threading.Lock()


def _scheduler_loop(interval_seconds: int) -> None:
    while True:
        time.sleep(interval_seconds)
        job = store.create("report", {"start_date": None, "end_date": None}, idempotency_key=None)
        enqueue(job.id)
        logger.info('{"scheduled_report_job_id": "%s"}', job.id)


def start_scheduler() -> None:
    global _started
    if os.environ.get("REPORT_SCHEDULE_ENABLED", "false").lower() != "true":
        return
    interval = int(os.environ.get("REPORT_SCHEDULE_INTERVAL_SECONDS", "86400"))  # default: daily
    with _start_lock:
        if _started:
            return
        t = threading.Thread(target=_scheduler_loop, args=(interval,), daemon=True)
        t.start()
        _started = True
        logger.info('{"scheduler_started": true, "interval_seconds": %d}', interval)