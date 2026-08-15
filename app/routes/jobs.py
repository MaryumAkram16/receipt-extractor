from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from ..jobs.store import store
from ..jobs.worker import enqueue

router = APIRouter()


class ExtractJobRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    # Client-supplied key so a retried POST (network blip, double-click,
    # at-least-once delivery from an upstream queue) doesn't enqueue the
    # same work twice. This is the idempotency half of "jobs will run twice".
    idempotency_key: Optional[str] = None


class ReportJobRequest(BaseModel):
    start_date: Optional[str] = None  # ISO date/datetime, inclusive lower bound
    end_date: Optional[str] = None    # ISO date/datetime, inclusive upper bound
    idempotency_key: Optional[str] = None


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


@router.post("/jobs/extract", status_code=202, response_model=JobAccepted)
def create_extract_job(req: ExtractJobRequest, response: Response):
    job = store.create("extract", {"text": req.text}, req.idempotency_key)
    if job.status == "pending" and job.attempts == 0:
        enqueue(job.id)
    response.headers["Location"] = f"/jobs/{job.id}"
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/jobs/report", status_code=202, response_model=JobAccepted)
def create_report_job(req: ReportJobRequest, response: Response):
    job = store.create(
        "report",
        {"start_date": req.start_date, "end_date": req.end_date},
        req.idempotency_key,
    )
    if job.status == "pending" and job.attempts == 0:
        enqueue(job.id)
    response.headers["Location"] = f"/jobs/{job.id}"
    return JobAccepted(job_id=job.id, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no job with that id")
    return JobStatus(
        job_id=job.id, kind=job.kind, status=job.status,
        attempts=job.attempts, result=job.result, error=job.error,
    )