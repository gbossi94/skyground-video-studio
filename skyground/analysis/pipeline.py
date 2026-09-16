"""Putting the pieces together: analyse, propose, answer, apply.

Four steps, each of which can be run on its own and re-run without redoing the
expensive one. Transcription costs money and minutes; proposing a cut costs
milliseconds. So the analysis is stored once and the plan is rebuilt from it
every time a question is answered — which also means the reasoning behind an
approved edit can still be inspected months later.
"""

from __future__ import annotations

import pathlib
import tempfile
from datetime import UTC, datetime

from skyground.analysis import audio, invariants
from skyground.analysis.adviser import Adviser, NullAdviser
from skyground.analysis.cut import ENGINE, CutPolicy, plan_cut
from skyground.analysis.models import Analysis, CutPlan
from skyground.analysis.transcription import Transcriber
from skyground.errors import ValidationError

ANALYSIS_DOCUMENT = "analysis.json"
CUTPLAN_DOCUMENT = "cutplan.json"


def analyze(
    source: pathlib.Path | str,
    *,
    transcriber: Transcriber,
    language: str = "it",
    source_label: str | None = None,
    workdir: pathlib.Path | None = None,
) -> Analysis:
    """Read a media file and return everything known about it, decided nothing."""
    source = pathlib.Path(source)
    if not source.is_file():
        raise ValidationError(f"sorgente non trovata: {source}")

    with tempfile.TemporaryDirectory(prefix="skyground-analysis-") as temporary:
        base = pathlib.Path(workdir or temporary)
        wav = audio.extract_audio(source, base / "audio16k.wav")
        duration = audio.probe_duration(source)
        words = transcriber.transcribe(wav, language=language)
        silences = audio.detect_silences(wav, duration=duration)

    return Analysis(
        source=source_label or source.name,
        duration=duration,
        words=words,
        silences=silences,
        language=language,
        provider=getattr(transcriber, "name", "sconosciuto"),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def propose(
    analysis: Analysis,
    *,
    policy: CutPolicy | None = None,
    decisions: dict[str, str] | None = None,
    adviser: Adviser | None = None,
    model=None,
) -> CutPlan:
    """Build a plan.

    With a `model`, the model edits: it reads the whole transcript, decides
    what stays, and re-reads the result (`editor.plan_edit`). Without one the
    heuristic engine runs — string similarity and hand-tuned scores — which is
    the engine that kept two attempts at the opening line back to back, and
    stays only for a studio without a key.
    """
    if model is not None:
        from skyground.analysis import editor

        return editor.plan_edit(analysis, model, decisions=decisions, policy=policy)

    adviser = adviser or NullAdviser()
    policy = policy or CutPolicy()
    from skyground.analysis import align
    from skyground.analysis.takes import build_utterances

    # The adviser answers with indices into the list of utterances it was shown,
    # so it has to be shown the same list the engine works from. Correcting the
    # timings here — once, before either of them looks — is what makes those
    # indices mean the same thing on both sides.
    analysis = align.prepare(analysis)
    utterances = build_utterances(analysis.words, gap=policy.utterance_gap)
    suspects = adviser.suspects(utterances) if utterances else []
    return plan_cut(analysis, policy, decisions or {}, suspects=suspects)


def decisions_from(plan: CutPlan) -> dict[str, str]:
    """The answers a *person* gave, so a rebuild does not ask them again.

    What the engine decided on its own is deliberately left out: those are not
    instructions, they are conclusions, and a rebuild should reach them again
    from the material — in the light of whatever the person has decided since.
    A choice made by hand is the one thing a rebuild must never overturn.
    """
    return {
        question.id: question.answer
        for question in plan.questions
        if question.answer is not None and question.answered_by != ENGINE
    }


def answer(
    analysis: Analysis,
    plan: CutPlan,
    question_id: str,
    option_id: str,
    *,
    policy: CutPolicy | None = None,
    adviser: Adviser | None = None,
    answered_by: str | None = None,
    model=None,
) -> CutPlan:
    """Record one decision and rebuild the plan around it."""
    question = next((item for item in plan.questions if item.id == question_id), None)
    if question is None:
        raise ValidationError(f"domanda non trovata: {question_id}")
    if question.option(option_id) is None:
        raise ValidationError(f"opzione non valida per {question_id}: {option_id}")

    decisions = decisions_from(plan)
    decisions[question_id] = option_id
    if plan.editor.get("decisions"):
        # The model already edited this film; lay the answer over its edit.
        from skyground.analysis import editor

        rebuilt = editor.replan(analysis, plan, decisions)
    else:
        rebuilt = propose(analysis, policy=policy, decisions=decisions, adviser=adviser, model=model)

    # Carry the answers onto the rebuilt questions so the record survives —
    # including *who* answered, which is the whole difference between a choice
    # the person made and one the engine reached on their behalf.
    was = {item.id: item.answered_by for item in plan.questions if item.answer is not None}
    for item in rebuilt.questions:
        if item.id in decisions:
            item.answer = decisions[item.id]
            item.answered_by = answered_by if item.id == question_id else was.get(item.id)
    _remember_answered(rebuilt, plan, decisions, answered_by, question_id)
    return rebuilt


def _remember_answered(
    rebuilt: CutPlan, previous: CutPlan, decisions: dict[str, str], answered_by, question_id
) -> None:
    """A question that disappears once answered still belongs in the record."""
    present = {item.id for item in rebuilt.questions}
    for item in previous.questions:
        if item.id in decisions and item.id not in present:
            item.answer = decisions[item.id]
            if item.id == question_id:
                item.answered_by = answered_by
            rebuilt.questions.append(item)
    rebuilt.questions.sort(key=lambda item: item.at)


def apply_to_timeline(plan: CutPlan, analysis: Analysis, timeline: dict) -> dict:
    """Turn an answered plan into `timeline.json`. Refuses anything else."""
    from skyground.analysis import align

    # Against the same corrected timings the plan was built from. Checked
    # against the raw ones it reported cuts landing inside words that, as the
    # engine sees them, end earlier — a true statement about data nobody uses,
    # and it blocked a perfectly good plan at the last step.
    ok, problems = invariants.applicable(
        plan, align.prepare(analysis), min_segment=float(plan.policy.get("min_segment", 0.35))
    )
    if not ok:
        raise ValidationError(
            "il piano non è applicabile:\n- " + "\n- ".join(problems)
        )
    updated = dict(timeline)
    updated["source"] = timeline.get("source") or analysis.source
    updated["clips"] = plan.to_timeline_clips()
    updated["duration"] = round(plan.output_duration, 6)
    updated["generatedBy"] = {
        "engine": "skyground-cut",
        "plan": plan.generated_at,
        "questions": len(plan.questions),
    }
    return updated
