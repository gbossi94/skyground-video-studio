"""The smart cut over HTTP.

Read the plan, answer a question, apply. Analysis is a job rather than a request
because transcribing six minutes of footage takes longer than any sane timeout.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from skyground.analysis.adviser import build_adviser
from skyground.analysis.editor import build_model
from skyground.api.deps import ProjectContext, get_settings, get_storage, project_context
from skyground.config import Settings
from skyground.errors import NotFound, ValidationError
from skyground.services import assets as asset_service
from skyground.services import cuts
from skyground.services import jobs as job_service
from skyground.storage import ObjectStorage

router = APIRouter()


def _editor_model(settings: Settings, override: dict | None = None):
    """The model that edits, when the studio is set to let one."""
    if settings.cut_engine != "editor":
        return None
    return build_model(settings, override)


@router.get("/api/projects/{slug}/cut")
def read_plan(
    context: ProjectContext = Depends(project_context),
    storage: ObjectStorage = Depends(get_storage),
) -> dict:
    """The current proposal, or what is missing before there can be one."""
    context.require("document:read")
    if not cuts.has_analysis(context.session, context.project):
        return {"state": "senza-analisi", "plan": None, "analysis": None}
    analysis = cuts.load_analysis(context.session, context.project)
    summary = {
        "source": analysis.source,
        "duration": analysis.duration,
        "words": len(analysis.words),
        "provider": analysis.provider,
        "generatedAt": analysis.generated_at,
        "proxyUrl": _proxy_url(context, storage),
    }
    try:
        plan = cuts.load_plan(context.session, context.project)
    except NotFound:
        return {"state": "senza-piano", "analysis": summary, "plan": None}
    return {"state": plan.status, "analysis": summary, "plan": plan.as_dict()}


@router.get("/api/projects/{slug}/cut/transcript")
def read_transcript(context: ProjectContext = Depends(project_context)) -> dict:
    """Word level timings, which the timeline needs to draw the speech."""
    context.require("document:read")
    analysis = cuts.load_analysis(context.session, context.project)
    return {
        "duration": analysis.duration,
        "words": [word.as_dict() for word in analysis.words],
        "silences": [silence.as_dict() for silence in analysis.silences],
    }


@router.post("/api/projects/{slug}/cut/analyze")
def start_analysis(
    payload: dict = Body(default={}), context: ProjectContext = Depends(project_context)
) -> dict:
    """Queue the transcription. The worker does the slow part."""
    context.require("job:create")
    job = job_service.enqueue(
        context.session,
        context.project,
        kind="analyze",
        payload={"source": (payload or {}).get("source") or ""},
        actor=context.user,
    )
    from skyground.api import serializers

    return serializers.job_payload(job)


@router.post("/api/projects/{slug}/cut/propose")
def propose(
    payload: dict = Body(default={}),
    context: ProjectContext = Depends(project_context),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Rebuild the proposal.

    Answers given by hand are carried over by default — overruling somebody's
    choice because they pressed regenerate would be the worst kind of surprise.
    `keepAnswers: false` starts from the material alone, which is the only way
    back once a choice has been made and turns out to have been wrong.
    """
    context.require("document:write")
    plan = cuts.propose(
        context.session,
        context.project,
        actor=context.user,
        adviser=build_adviser(settings),
        model=_editor_model(settings, (payload or {}).get("editor")),
        keep_answers=bool((payload or {}).get("keepAnswers", True)),
    )
    return {"state": plan.status, "plan": plan.as_dict()}


@router.post("/api/projects/{slug}/cut/full")
def full_cut(
    payload: dict = Body(default={}),
    context: ProjectContext = Depends(project_context),
) -> dict:
    """Queue the whole thing: hear, decide, cut, rebuild, render, publish.

    `editor` picks the model for this run; `render: false` stops before the
    render; `reanalyze: true` transcribes again even when an analysis exists.
    """
    context.require("job:create")
    job = job_service.enqueue(
        context.session,
        context.project,
        kind="full",
        payload={k: v for k, v in (payload or {}).items() if k in ("editor", "render", "reanalyze", "fresh")},
        actor=context.user,
        max_attempts=1,
    )
    from skyground.api import serializers

    return serializers.job_payload(job)


@router.post("/api/projects/{slug}/cut/questions/{question_id}")
def answer_question(
    question_id: str,
    payload: dict = Body(...),
    context: ProjectContext = Depends(project_context),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Resolve one ambiguity. The plan is rebuilt around the answer."""
    context.require("document:write")
    option = (payload or {}).get("option")
    if not option:
        raise ValidationError("indica quale opzione hai scelto")
    plan = cuts.answer(
        context.session,
        context.project,
        question_id,
        option,
        actor=context.user,
        adviser=build_adviser(settings),
        model=_editor_model(settings),
    )
    return {"state": plan.status, "plan": plan.as_dict()}


@router.post("/api/projects/{slug}/cut/apply")
def apply_plan(context: ProjectContext = Depends(project_context)) -> dict:
    """Turn an answered plan into the project's timeline."""
    context.require("document:write")
    return cuts.apply(
        context.session, context.project, context.workspace, actor=context.user
    )


def _proxy_url(context: ProjectContext, storage: ObjectStorage) -> str | None:
    """A browser playable copy of the take, when the worker has made one.

    The camera original is usually HEVC, which no browser decodes; the editor
    falls back to the original only because a project may already hold an H.264
    master.
    """
    # Asking storage rather than the asset index: the bytes are what the player
    # needs, and one source of truth cannot disagree with itself.
    for name in ("source.mp4", "source.webm"):
        key = asset_service.object_key(context.project, f"proxy/{name}")
        if storage.exists(key):
            return storage.signed_url(key)
    return None
