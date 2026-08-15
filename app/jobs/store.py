"""In-memory, thread-safe store for background job state.

No external infra — just a dict behind a lock. Good enough for one process;
the moment this needs to survive a restart or run across multiple processes,
it's the first thing to swap for Redis.

Generic over job "kind" — the same store, queue, retry policy, and alerting
serve both /jobs/extract and /jobs/report, rather than duplicating the
pattern per job type.
"""
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Job:
    id: str
    kind: str  # "extract" | "report"
    status: str  # "pending" | "running" | "succeeded" | "failed"
    input_data: dict[str, Any]
    idempotency_key: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._idempotency_index: dict[str, str] = {}  # idempotency_key -> job_id
        self._lock = threading.Lock()

    def create(self, kind: str, input_data: dict[str, Any], idempotency_key: Optional[str]) -> Job:
        with self._lock:
            if idempotency_key:
                existing_id = self._idempotency_index.get(idempotency_key)
                if existing_id:
                    return self._jobs[existing_id]

            job = Job(id=str(uuid.uuid4()), kind=kind, status="pending", input_data=input_data,
                       idempotency_key=idempotency_key)
            self._jobs[job.id] = job
            if idempotency_key:
                self._idempotency_index[idempotency_key] = job.id
            return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()


store = JobStore()