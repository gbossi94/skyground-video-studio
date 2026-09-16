"""Render job queue.

The queue lives in PostgreSQL and is claimed with `SELECT … FOR UPDATE SKIP
LOCKED`, which is exactly-once across any number of workers and removes the need
for a separate broker in this phase. On SQLite the same code path falls back to a
plain transaction, which is enough for a single local worker.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.db.models import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    Project,
    RenderJob,
    User,
    utcnow,
)
from skyground.errors import NotFound, ValidationError
from skyground.services import audit

#: Work the worker knows how to run. Phase 3 extends this list.
JOB_KINDS = ("render", "sync", "validate", "analyze", "full", "proxy", "transcribe")

RETRY_BACKOFF_SECONDS = (30, 120, 600)


def enqueue(
    session: Session,
    project: Project,
    *,
    kind: str = "render",
    payload: dict | None = None,
    actor: User | None = None,
    priority: int = 0,
    max_attempts: int = 3,
) -> RenderJob:
    if kind not in JOB_KINDS:
        raise ValidationError(f"tipo di job non supportato: {kind}")
    job = RenderJob(
        project_id=project.id,
        kind=kind,
        status=JOB_QUEUED,
        payload=payload or {},
        priority=priority,
        max_attempts=max_attempts,
        requested_by=actor.id if actor else None,
        available_at=utcnow(),
    )
    session.add(job)
    session.flush()
    audit.record(
        session, "job.enqueue", actor=actor, project=project, target=kind, data={"job": job.id}
    )
    return job


def get(session: Session, job_id: str) -> RenderJob:
    job = session.get(RenderJob, job_id)
    if job is None:
        raise NotFound(f"job non trovato: {job_id}")
    return job


def list_for_project(session: Session, project: Project, limit: int = 50) -> list[RenderJob]:
    return list(
        session.scalars(
            select(RenderJob)
            .where(RenderJob.project_id == project.id)
            .order_by(RenderJob.created_at.desc())
            .limit(limit)
        )
    )


def claim(
    session: Session, worker: str, *, kinds: tuple[str, ...] | None = None
) -> RenderJob | None:
    """Take the next due job, or return None when the queue is empty."""
    query = (
        select(RenderJob)
        .where(
            RenderJob.status == JOB_QUEUED,
            RenderJob.available_at <= utcnow(),
        )
        .order_by(RenderJob.priority.desc(), RenderJob.available_at, RenderJob.created_at)
        .limit(1)
    )
    if kinds:
        query = query.where(RenderJob.kind.in_(kinds))
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)

    job = session.scalar(query)
    if job is None:
        return None
    job.status = JOB_RUNNING
    job.attempts += 1
    job.started_at = utcnow()
    job.locked_by = worker[:120]
    job.locked_at = utcnow()
    session.flush()
    return job


def succeed(session: Session, job: RenderJob, result: dict | None = None) -> RenderJob:
    job.status = JOB_SUCCEEDED
    job.result = result or {}
    job.error = None
    job.finished_at = utcnow()
    job.locked_by = None
    job.locked_at = None
    session.flush()
    audit.record(
        session,
        "job.succeed",
        project=session.get(Project, job.project_id),
        target=job.kind,
        data={"job": job.id, "result": job.result},
    )
    return job


def fail(session: Session, job: RenderJob, error: str, *, retry: bool = True) -> RenderJob:
    """Fail a job, scheduling a retry while attempts remain."""
    job.error = (error or "")[:4000]
    job.locked_by = None
    job.locked_at = None
    if retry and job.attempts < job.max_attempts:
        delay = RETRY_BACKOFF_SECONDS[min(job.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        job.status = JOB_QUEUED
        job.available_at = utcnow() + timedelta(seconds=delay)
        job.started_at = None
    else:
        job.status = JOB_FAILED
        job.finished_at = utcnow()
    session.flush()
    audit.record(
        session,
        "job.fail",
        project=session.get(Project, job.project_id),
        target=job.kind,
        data={"job": job.id, "status": job.status, "attempts": job.attempts},
    )
    return job


def cancel(session: Session, job: RenderJob, *, actor: User | None = None) -> RenderJob:
    if job.status in (JOB_SUCCEEDED, JOB_FAILED):
        raise ValidationError("un job concluso non può essere annullato")
    job.status = JOB_CANCELLED
    job.finished_at = utcnow()
    job.locked_by = None
    job.locked_at = None
    session.flush()
    audit.record(
        session,
        "job.cancel",
        actor=actor,
        project=session.get(Project, job.project_id),
        target=job.kind,
        data={"job": job.id},
    )
    return job


def release(session: Session, job: RenderJob, reason: str) -> RenderJob:
    """Hand a running job back to the queue untouched.

    The worker is being stopped — a deploy is replacing its container — and
    will not get to finish. The job goes back to the front of the queue with
    its attempt returned, because nothing failed: the next worker starts it
    over.
    """
    job.status = JOB_QUEUED
    job.error = reason[:4000]
    job.attempts = max(job.attempts - 1, 0)
    job.available_at = utcnow()
    job.started_at = None
    job.locked_by = None
    job.locked_at = None
    session.flush()
    audit.record(
        session,
        "job.release",
        project=session.get(Project, job.project_id),
        target=job.kind,
        data={"job": job.id, "reason": reason},
    )
    return job


def reap_orphans(session: Session, worker: str) -> int:
    """Requeue the jobs a previous worker of this same host left running.

    A worker is named `host:pid`. When the container restarts — the browser
    renderer ran the host out of memory, a deploy landed mid-job — the new
    worker starts on the same host with a new pid, and whatever the old one
    held is still marked running with nobody working on it. Waiting for the
    stall timeout would leave the film an hour late; the host itself is the
    proof that the old worker is gone.
    """
    host = worker.rsplit(":", 1)[0]
    orphans = session.scalars(
        select(RenderJob).where(
            RenderJob.status == JOB_RUNNING,
            RenderJob.locked_by.like(f"{host}:%"),
            RenderJob.locked_by != worker,
        )
    ).all()
    for job in orphans:
        fail(session, job, f"il worker {job.locked_by} è stato riavviato mentre lavorava")
    return len(orphans)


def reap_stalled(session: Session, *, timeout_seconds: int = 3600) -> int:
    """Requeue jobs whose worker died while holding them."""
    deadline = utcnow() - timedelta(seconds=timeout_seconds)
    stalled = session.scalars(
        select(RenderJob).where(RenderJob.status == JOB_RUNNING, RenderJob.locked_at < deadline)
    ).all()
    for job in stalled:
        fail(session, job, f"worker {job.locked_by} non ha risposto entro {timeout_seconds}s")
    return len(stalled)
