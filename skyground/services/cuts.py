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

from skyground.analysis import pipeline
from skyground.analysis.adviser import Adviser, NullAdviser
from skyground.analysis.cut import CutPolicy
from skyground.analysis.models import Analysis, CutPlan
from skyground.core.workspace import Workspace, content_digest
from skyground.db.models import Document, Project, User
from skyground.errors import NotFound, ValidationError
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


def propose(
    session: Session,
    project: Project,
    *,
    actor: User | None = None,
    adviser: Adviser | None = None,
    keep_answers: bool = True,
) -> CutPlan:
    """Generate a plan, carrying over decisions already made."""
    analysis = load_analysis(session, project)
    decisions = {}
    if keep_answers:
        try:
            decisions = pipeline.decisions_from(load_plan(session, project))
        except NotFound:
            decisions = {}
    plan = pipeline.propose(
        analysis,
        policy=policy_for(project),
        decisions=decisions,
        adviser=adviser or NullAdviser(),
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
) -> CutPlan:
    analysis = load_analysis(session, project)
    plan = load_plan(session, project)
    updated = pipeline.answer(
        analysis,
        plan,
        question_id,
        option_id,
        policy=policy_for(project),
        adviser=adviser or NullAdviser(),
        answered_by=actor.email if actor else None,
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


def apply(
    session: Session,
    project: Project,
    workspace: Workspace,
    *,
    actor: User | None = None,
) -> dict:
    """Write the plan into `timeline.json`, through the normal document path.

    Going through `DocumentService` rather than writing the file directly is
    what gives the change a revision, an author and an audit entry — an edit
    made by the engine is reviewable exactly like one made by hand.
    """
    analysis = load_analysis(session, project)
    plan = load_plan(session, project)
    if plan.open_questions:
        raise ValidationError(
            f"restano {len(plan.open_questions)} domande senza risposta: "
            "il montaggio non viene applicato finché non sono risolte"
        )

    documents = DocumentService(session, workspace)
    timeline = documents.read(project, "timeline.json").content
    updated = pipeline.apply_to_timeline(plan, analysis, timeline)
    state = documents.write(
        project,
        "timeline.json",
        updated,
        actor=actor,
        message=f"montaggio automatico: {len(updated['clips'])} clip, "
        f"{updated['duration']:.1f}s",
    )

    manifest = dict(documents.read(project, "project.json").content)
    canvas = dict(manifest.get("canvas", {}))
    canvas["duration"] = updated["duration"]
    manifest["canvas"] = canvas
    documents.write(
        project, "project.json", manifest, actor=actor, message="durata dal montaggio automatico"
    )

    plan.applied_at = datetime.now(UTC).isoformat(timespec="seconds")
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
        "problems": documents.problems(project),
    }
