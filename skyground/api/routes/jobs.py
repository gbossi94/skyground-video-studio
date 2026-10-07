"""Render jobs: enqueue, follow, cancel."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from skyground.api import serializers
from skyground.api.deps import ProjectContext, project_context
from skyground.errors import NotFound
from skyground.services import jobs as job_service

router = APIRouter()


@router.get("/api/projects/{slug}/jobs")
def list_jobs(limit: int = 50, context: ProjectContext = Depends(project_context)) -> list[dict]:
    context.require("job:read")
    jobs = job_service.list_for_project(context.session, context.project, limit=min(limit, 200))
    return [serializers.job_payload(job) for job in jobs]


@router.post("/api/projects/{slug}/jobs")
def create_job(
    payload: dict = Body(default={}), context: ProjectContext = Depends(project_context)
) -> dict:
    context.require("job:create")
    job = job_service.enqueue(
        context.session,
        context.project,
        kind=(payload or {}).get("kind", "render"),
        payload=(payload or {}).get("payload") or {},
        actor=context.user,
    )
    return serializers.job_payload(job)


@router.get("/api/projects/{slug}/jobs/{job_id}")
def read_job(job_id: str, context: ProjectContext = Depends(project_context)) -> dict:
    context.require("job:read")
    job = job_service.get(context.session, job_id)
    # A job id from another project must not be readable through this project.
    if job.project_id != context.project.id:
        raise NotFound(f"job non trovato: {job_id}")
    return serializers.job_payload(job)


@router.post("/api/projects/{slug}/jobs/{job_id}/cancel")
def cancel_job(job_id: str, context: ProjectContext = Depends(project_context)) -> dict:
    context.require("job:cancel")
    job = job_service.get(context.session, job_id)
    if job.project_id != context.project.id:
        raise NotFound(f"job non trovato: {job_id}")
    return serializers.job_payload(job_service.cancel(context.session, job, actor=context.user))
