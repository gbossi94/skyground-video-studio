"""Storing the analysis and the cut plan of a project.

Both are *derived* data, not editorial source: the analysis can be rebuilt by
transcribing again, the plan by re-running the engine over it. So they live in
the database rather than in the Git checkout — which also keeps a client's
outtakes out of the repository, where only the finished captions belong.

The one piece of real state here is the answers: those are decisions a person
made, and they are what turns a draft into something applicable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.analysis import manual, pipeline
from skyground.analysis.adviser import Adviser, NullAdviser
from skyground.analysis.cut import CutPolicy
from skyground.analysis.models import Analysis, CutPlan
from skyground.core import retime
from skyground.core.workspace import Workspace, content_digest
from skyground.db.models import Document, Project, User
from skyground.errors import Conflict, NotFound, ValidationError
from skyground.services import audit
from skyground.services.documents import DocumentService

ANALYSIS = "analysis.json"
CUTPLAN = "cutplan.json"
DERIVED = (ANALYSIS, CUTPLAN)


def _row(session: Session, project: Project, name: str) -> Document | None:
    return session.scalar(
        select(Document).where(Document.project_id == project.id, Document.name == name)
    )


def _write(session: Session, project: Project, name: str, content: dict, actor: User | None):
    document = _row(session, project, name)
    if document is None:
        # Column defaults only apply at insert time, so the counter starts here.
        document = Document(project_id=project.id, name=name, revision_number=0, etag="")
        session.add(document)
    document.content = content
    document.etag = content_digest(content)
    document.revision_number = (document.revision_number or 0) + 1
    document.updated_by = actor.id if actor else None
    session.flush()
    return document


# -------------------------------------------------------------------- analysis


def save_analysis(
    session: Session, project: Project, analysis: Analysis, *, actor: User | None = None
) -> Analysis:
    _write(session, project, ANALYSIS, analysis.as_dict(), actor)
    audit.record(
        session,
        "cut.analyze",
        actor=actor,
        project=project,
        target=analysis.source,
        data={"words": len(analysis.words), "provider": analysis.provider},
    )
    return analysis


def load_analysis(session: Session, project: Project) -> Analysis:
    document = _row(session, project, ANALYSIS)
    if document is None or not document.content:
        raise NotFound(
            "il girato non è ancora stato analizzato: esegui prima il job 'analyze'"
        )
    return Analysis.from_dict(document.content)


def has_analysis(session: Session, project: Project) -> bool:
    document = _row(session, project, ANALYSIS)
    return bool(document and document.content)


# ------------------------------------------------------------------- the plan


def save_plan(
    session: Session, project: Project, plan: CutPlan, *, actor: User | None = None
) -> CutPlan:
    _write(session, project, CUTPLAN, plan.as_dict(), actor)
    return plan


def load_plan(session: Session, project: Project) -> CutPlan:
    document = _row(session, project, CUTPLAN)
    if document is None or not document.content:
        raise NotFound("nessun piano di taglio: generane uno")
    return CutPlan.from_dict(document.content)


def policy_for(project: Project) -> CutPolicy:
    """A project may tune the engine; unset fields keep the defaults."""
    stored = (project.settings or {}).get("cutPolicy") or {}
    known = {field: stored[field] for field in CutPolicy().as_dict() if field in stored}
    return CutPolicy(**known)


def plan_etag(session: Session, project: Project) -> str | None:
    """The digest of the stored plan: what a client sends back as `If-Match`."""
    document = _row(session, project, CUTPLAN)
    return document.etag if document and document.content else None


def has_manual(session: Session, project: Project) -> bool:
    document = _row(session, project, CUTPLAN)
    return bool(document and document.content and (document.content.get("manual") or {}))


def _policy_of(plan: CutPlan, project: Project) -> CutPolicy:
    """The policy the plan was built with, which is the one its edits obey."""
    known = CutPolicy().as_dict()
    stored = {key: value for key, value in (plan.policy or {}).items() if key in known}
    return CutPolicy(**stored) if stored else policy_for(project)


def _check_etag(session: Session, project: Project, plan: CutPlan, base_etag: str | None) -> None:
    current = plan_etag(session, project)
    if base_etag and current and base_etag != current:
        raise Conflict(
            "il montaggio è cambiato dopo il caricamento", current=plan.as_dict(), etag=current
        )


# ------------------------------------------------------------- the hand edit


def edit(
    session: Session,
    project: Project,
    kept: list[manual.Kept],
    *,
    actor: User | None = None,
    base_etag: str | None = None,
    workspace: Workspace | None = None,
) -> CutPlan:
    """The cut as a person left it on the timeline, realised and stored.

    `base_etag` is the plan the client edited: when the stored plan has moved
    on since — another tab, another person — the write is refused with the
    current plan, never merged in silence.
    """
    analysis = load_analysis(session, project)
    try:
        plan = load_plan(session, project)
    except NotFound as error:
        raise ValidationError("prima serve una proposta del motore da correggere") from error
    _check_etag(session, project, plan, base_etag)
    timeline_etag = None
    if workspace is not None:
        try:
            timeline_etag = DocumentService(session, workspace).read(project, "timeline.json").etag
        except NotFound:
            timeline_etag = None
    updated = manual.realise(
        analysis,
        plan,
        kept,
        policy=_policy_of(plan, project),
        edited_by=actor.email if actor else None,
        timeline_etag=timeline_etag,
    )
    save_plan(session, project, updated, actor=actor)
    audit.record(
        session,
        "cut.edit",
        actor=actor,
        project=project,
        data={
            "segments": len(updated.segments),
            "removedManual": sum(1 for item in updated.removed if item.reason == "manual"),
            "duration": round(updated.output_duration, 3),
        },
    )
    return updated


def edit_from_timeline(
    session: Session,
    project: Project,
    workspace: Workspace,
    *,
    actor: User | None = None,
    base_etag: str | None = None,
) -> tuple[CutPlan, list[str]]:
    """The applied timeline — hand written, or an old revision restored —
    brought back onto the plan so it can be corrected from there."""
    analysis = load_analysis(session, project)
    timeline = DocumentService(session, workspace).read(project, "timeline.json").content
    kept, notes = manual.from_timeline(analysis, timeline.get("clips", []))
    plan = edit(session, project, kept, actor=actor, base_etag=base_etag, workspace=workspace)
    return plan, notes


def propose(
    session: Session,
    project: Project,
    *,
    actor: User | None = None,
    adviser: Adviser | None = None,
    model=None,
    keep_answers: bool = True,
) -> CutPlan:
    """Generate a plan, carrying over decisions already made."""
    analysis = load_analysis(session, project)
    decisions = {}
    if keep_answers:
        try:
            previous = load_plan(session, project)
        except NotFound:
            previous = None
        if previous is not None and previous.manual:
            raise ValidationError(
                "questo montaggio è stato corretto a mano: rigenerarlo lo perderebbe. "
                "«Riparti da zero» butta via le correzioni e ricalcola dal girato"
            )
        decisions = pipeline.decisions_from(previous) if previous is not None else {}
    if model is None:
        policy = policy_for(project)
    else:
        from skyground.analysis import editor

        policy = editor.policy_from((project.settings or {}).get("cutPolicy"))
    plan = pipeline.propose(
        analysis,
        policy=policy,
        decisions=decisions,
        adviser=adviser or NullAdviser(),
        model=model,
    )
    save_plan(session, project, plan, actor=actor)
    audit.record(
        session,
        "cut.propose",
        actor=actor,
        project=project,
        data={"stats": plan.stats(), "status": plan.status},
    )
    return plan


def answer(
    session: Session,
    project: Project,
    question_id: str,
    option_id: str,
    *,
    actor: User | None = None,
    adviser: Adviser | None = None,
    model=None,
) -> CutPlan:
    analysis = load_analysis(session, project)
    plan = load_plan(session, project)
    if plan.manual:
        updated = _answer_on_the_timeline(analysis, plan, project, question_id, option_id, actor)
    else:
        updated = pipeline.answer(
            analysis,
            plan,
            question_id,
            option_id,
            policy=policy_for(project),
            adviser=adviser or NullAdviser(),
            answered_by=actor.email if actor else None,
            model=model,
        )
    save_plan(session, project, updated, actor=actor)
    audit.record(
        session,
        "cut.answer",
        actor=actor,
        project=project,
        target=question_id,
        data={"option": option_id, "status": updated.status},
    )
    return updated


def _answer_on_the_timeline(analysis, plan, project, question_id, option_id, actor) -> CutPlan:
    """With a hand edit in place, an answer is a change to the kept ranges:
    the engine's `edit:` decisions can still be flipped, everything else is
    decided on the timeline itself."""
    question = next((item for item in plan.questions if item.id == question_id), None)
    if question is None:
        raise ValidationError(f"domanda non trovata: {question_id}")
    if question.option(option_id) is None:
        raise ValidationError(f"opzione non valida per {question_id}: {option_id}")
    if not question_id.startswith("edit:"):
        raise ValidationError(
            "con un montaggio corretto a mano le decisioni si prendono sulla timeline"
        )
    first, last = (int(part) for part in question_id[5:].split("-"))
    kept = [manual.Kept.from_dict(item) for item in plan.manual.get("kept", [])]
    kept = manual.with_answer(kept, first, last, keep=(option_id == "keep"))
    return manual.realise(
        analysis,
        plan,
        kept,
        policy=_policy_of(plan, project),
        edited_by=actor.email if actor else None,
    )


def apply(
    session: Session,
    project: Project,
    workspace: Workspace,
    *,
    actor: User | None = None,
    base_etag: str | None = None,
) -> dict:
    """Write the plan into `timeline.json`, through the normal document path.

    Going through `DocumentService` rather than writing the file directly is
    what gives the change a revision, an author and an audit entry — an edit
    made by the engine is reviewable exactly like one made by hand.
    """
    analysis = load_analysis(session, project)
    plan = load_plan(session, project)
    _check_etag(session, project, plan, base_etag)
    if plan.open_questions and not plan.manual:
        raise ValidationError(
            f"restano {len(plan.open_questions)} domande senza risposta: "
            "il montaggio non viene applicato finché non sono risolte"
        )

    documents = DocumentService(session, workspace)
    timeline = documents.read(project, "timeline.json").content
    updated = pipeline.apply_to_timeline(plan, analysis, timeline)
    # Onto whole frames before anything is timed against it. Doing this at
    # encode time instead, and writing the result back, is what put the
    # captions on the wrong frames: they had been placed against numbers the
    # encoder was about to change.
    manifest_now = documents.read(project, "project.json").content
    fps = int(manifest_now.get("canvas", {}).get("fps", 30))
    updated["duration"] = retime.snap_to_frames(updated["clips"], fps)
    how = "montaggio corretto a mano" if plan.manual else "montaggio automatico"
    state = documents.write(
        project,
        "timeline.json",
        updated,
        actor=actor,
        message=f"{how}: {len(updated['clips'])} clip, {updated['duration']:.1f}s",
    )

    manifest = dict(documents.read(project, "project.json").content)
    canvas = dict(manifest.get("canvas", {}))
    canvas["duration"] = updated["duration"]
    manifest["canvas"] = canvas
    documents.write(
        project, "project.json", manifest, actor=actor, message="durata dal montaggio automatico"
    )

    # Everything written in output time now points at the wrong frames. Moving
    # it is part of applying the cut, not a separate errand somebody remembers:
    # forgetting it renders a film whose subtitles run ahead of the voice.
    lost = _move_the_layers(
        documents, project, timeline.get("clips", []), updated["clips"], analysis, actor
    )

    plan.applied_at = datetime.now(UTC).isoformat(timespec="seconds")
    if plan.manual:
        plan.manual["appliedRevision"] = state.revision
        plan.manual["basedOnTimelineEtag"] = state.etag
    save_plan(session, project, plan, actor=actor)
    audit.record(
        session,
        "cut.apply",
        actor=actor,
        project=project,
        data={"clips": len(updated["clips"]), "duration": updated["duration"]},
    )
    return {
        "applied": True,
        "clips": len(updated["clips"]),
        "duration": updated["duration"],
        "revision": state.revision,
        "dropped": lost,
        "problems": documents.problems(project),
    }


def _move_the_layers(
    documents: DocumentService,
    project: Project,
    old_clips: list[dict],
    new_clips: list[dict],
    analysis: Analysis,
    actor: User | None,
) -> list[str]:
    """Re-time captions, cards and angles onto the new cut. Returns what fell out."""
    from skyground.analysis import align

    words = align.prepare(analysis).words
    documents.write(
        project,
        "captions.json",
        retime.captions_from(words, new_clips),
        actor=actor,
        message="sottotitoli riallineati al nuovo montaggio",
    )

    lost: list[str] = []
    for name, move, key in (
        ("cards.json", retime.move_cards, "card"),
        ("angles.json", retime.move_angles, "inserto"),
    ):
        try:
            current = documents.read(project, name).content
        except NotFound:
            continue
        moved, gone = move(current, old_clips, new_clips)
        lost.extend(f"{key} {line}" for line in gone)
        documents.write(
            project, name, moved, actor=actor, message="riallineati al nuovo montaggio"
        )
    return lost
