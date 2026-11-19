from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from ..db import normalize_bound
from ..jobs.store import IdempotencyConflict, store
from ..jobs.worker import enqueue
from ..llm.schema import ExtractRequest

router = APIRouter()

IDEMPOTENCY_KEY_MAX_LENGTH = 255


def _normalize_key(v: Optional[str]) -> Optional[str]:
    # An empty key has always behaved like "no key"; keep it that way.
    if v is None or v == "":
        return None
    return v


class ExtractJobRequest(ExtractRequest):
    # Inherits text's 1-4000 length limit *and* the not-blank check from the
    # sync endpoint, so both entry points accept exactly the same input.

    # Client-supplied key so a retried POST (network blip, double-click,
    # at-least-once delivery from an upstream queue) doesn't enqueue the
    # same work twice. This is the idempotency half of "jobs will run twice".
    idempotency_key: Optional[str] = Field(default=None, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)

    @field_validator("idempotency_key")
    @classmethod
    def _key(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_key(v)


class ReportJobRequest(BaseModel):
    start_date: Optional[str] = None  # ISO date/datetime, inclusive lower bound
    end_date: Optional[str] = None    # ISO date/datetime, inclusive upper bound (a bare date covers the whole day)
    idempotency_key: Optional[str] = Field(default=None, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)

    @field_validator("idempotency_key")
    @classmethod
    def _key(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_key(v)

    @field_validator("start_date")
    @classmethod
    def _start(cls, v: Optional[str]) -> Optional[str]:
        return normalize_bound(v, end=False)

    @field_validator("end_date")
    @classmethod
    def _end(cls, v: Optional[str]) -> Optional[str]:
        return normalize_bound(v, end=True)

    @model_validator(mode="after")
    def _ordered(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class JobAccepted(BaseModel):
    job_id: str
    status: str


class JobStatus(BaseModel):
    job_id: str
    kind: str
    status: str
    attempts: int
    result: Optional[dict] = None
    error: Optional[str] = None
    error_category: Optional[str] = None


def _submit(kind: str, payload: dict, idempotency_key: Optional[str], response: Response) -> JobAccepted:
    try:
        job, created = store.create_or_get(kind, payload, idempotency_key)
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail="idempotency_key was already used with a different request payload",
        )
    # Only a newly created job goes on the queue. Replays of an existing key
    # return the original job without enqueueing it a second time.
    if created:
        enqueue(job.id)
    response.headers["Location"] = f"/jobs/{job.id}"
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/jobs/extract", status_code=202, response_model=JobAccepted)
def create_extract_job(req: ExtractJobRequest, response: Response):
    return _submit("extract", {"text": req.text}, req.idempotency_key, response)


@router.post("/jobs/report", status_code=202, response_model=JobAccepted)
def create_report_job(req: ReportJobRequest, response: Response):
    return _submit(
        "report",
        {"start_date": req.start_date, "end_date": req.end_date},
        req.idempotency_key,
        response,
    )


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no job with that id")
    return JobStatus(
        job_id=job.id, kind=job.kind, status=job.status,
        attempts=job.attempts, result=job.result, error=job.error,
        error_category=job.error_category,
    )
