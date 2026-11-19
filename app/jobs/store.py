"""In-memory, thread-safe store for background job state.

No external infra — just a dict behind a lock. Good enough for one process;
the moment this needs to survive a restart or run across multiple processes,
it's the first thing to swap for Redis.

Generic over job "kind" — the same store, queue, retry policy, and alerting
serve both /jobs/extract and /jobs/report, rather than duplicating the
pattern per job type.

Idempotency rules (all decided under one lock, so concurrent requests with
the same key cannot create two jobs):

    no key                         -> always a new job
    same key + same payload        -> the original job (created=False)
    same key + different payload   -> IdempotencyConflict (HTTP 409)

Keys are scoped per job kind: "abc" on /jobs/extract and "abc" on
/jobs/report are unrelated. The payload comparison uses a SHA-256 over
canonical JSON of {kind, payload}, so key order and whitespace in the
request body don't matter.

Limitation: everything here, idempotency records included, lives in process
memory and is lost on restart. A key replayed after a restart creates a new
job.
"""
import dataclasses
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different request payload."""

    def __init__(self, existing_job_id: str) -> None:
        super().__init__("idempotency key already used with a different request payload")
        self.existing_job_id = existing_job_id


def request_fingerprint(kind: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Job:
    id: str
    kind: str  # "extract" | "report"
    status: str  # "pending" | "running" | "succeeded" | "failed"
    input_data: dict[str, Any]
    idempotency_key: Optional[str] = None
    request_fingerprint: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    error_category: Optional[str] = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._idempotency_index: dict[tuple[str, str], str] = {}  # (kind, key) -> job_id
        self._lock = threading.Lock()

    def create_or_get(
        self, kind: str, input_data: dict[str, Any], idempotency_key: Optional[str]
    ) -> tuple[Job, bool]:
        """Returns (job, created). `created` is False when an earlier job with
        the same key and payload was returned — callers must only enqueue
        when created is True, or the same job would run twice."""
        fingerprint = request_fingerprint(kind, input_data)
        with self._lock:
            if idempotency_key:
                existing_id = self._idempotency_index.get((kind, idempotency_key))
                if existing_id is not None:
                    existing = self._jobs[existing_id]
                    if existing.request_fingerprint != fingerprint:
                        raise IdempotencyConflict(existing_id)
                    return dataclasses.replace(existing), False

            job = Job(
                id=str(uuid.uuid4()),
                kind=kind,
                status=PENDING,
                input_data=dict(input_data),
                idempotency_key=idempotency_key or None,
                request_fingerprint=fingerprint,
            )
            self._jobs[job.id] = job
            if idempotency_key:
                self._idempotency_index[(kind, idempotency_key)] = job.id
            return dataclasses.replace(job), True

    def create(self, kind: str, input_data: dict[str, Any], idempotency_key: Optional[str] = None) -> Job:
        """Backward-compatible wrapper around create_or_get (used by the scheduler)."""
        job, _ = self.create_or_get(kind, input_data, idempotency_key)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        """Returns a snapshot copy, so readers never see a half-applied update."""
        with self._lock:
            job = self._jobs.get(job_id)
            return dataclasses.replace(job) if job is not None else None

    def claim(self, job_id: str) -> Optional[Job]:
        """Atomically move a job from pending to running. Returns None if the
        job doesn't exist or another worker already claimed it — the guard
        that makes an accidental duplicate enqueue harmless."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != PENDING:
                return None
            job.status = RUNNING
            job.updated_at = time.time()
            return dataclasses.replace(job)

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()

    def clear(self) -> None:
        """Drop all jobs and idempotency records (used by tests)."""
        with self._lock:
            self._jobs.clear()
            self._idempotency_index.clear()


store = JobStore()
