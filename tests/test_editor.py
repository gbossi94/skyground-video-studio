"""The editor: the model decides what is said, the code decides where to cut.

These tests use a scripted model — one that answers what the test tells it to —
because what is under test is everything around the model: that its answer is
read carefully, repaired when it is malformed, realised into cuts that land in
silence, read back for review, and never asked twice for the same film.
"""

from __future__ import annotations

import pytest

from skyground.analysis import editor, invariants, pipeline
from skyground.analysis.models import Analysis, CutPlan, Word


def speak(lines: list[str], *, start: float = 0.0, pace: float = 0.32, gap: float = 1.0) -> list[Word]:
    """Lines of text as timed words: a short gap inside a line, a breath between."""
    words: list[Word] = []
    at = start
    for line in lines:
        for token in line.split():
            words.append(Word(t=at, end=at + 0.26, s=token, p=0.95))
            at += pace
        at += gap
    return words


def analysis_of(lines: list[str]) -> Analysis:
    words = speak(lines)
    return Analysis(source="raw.mov", duration=words[-1].end + 2.0, words=words)


class Scripted:
    """Answers in order; complains if asked more than it was told."""

    name = "finto"

    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    def ask(self, system: str, user: str, schema: dict) -> dict:
        self.asked.append((system, user))
        if not self.answers:
            raise AssertionError("il modello è stato interrogato più del previsto")
        return self.answers.pop(0)


OK = {"verdict": "ok", "revisions": [], "notes": "pulito"}

#: The reference footage's opening, the case the old engine could not see: the
#: speaker restarts *mid-sentence*, so there is no shared opening to match on.
OPENING = [
    "Se il tuo centro estetico è bloccato tra i 10 e i 20 mila euro al mese, sei nel famosissimo fango.",
    "20 mila euro al mese, sei in quello che io chiamo il fango.",
    "Non importa quanto premi sull'acceleratore, non ti muovi.",
]


# ------------------------------------------------------------ reading the answer


def test_the_transcript_is_shown_with_every_word_numbered():
    analysis = analysis_of(["ciao a tutti", "oggi parliamo"])
    from skyground.analysis import align, takes

    prepared = align.prepare(analysis)
    text = editor.present(prepared.words, takes.build_utterances(prepared.words, gap=0.55))
    assert "0:ciao 1:a 2:tutti" in text
    assert "3:oggi 4:parliamo" in text


def test_a_clean_answer_becomes_decisions_that_cover_every_word():
    words = speak(["uno due tre", "quattro cinque"])
    edit = editor.parse_edit(
        {"segments": [
            {"first": 0, "last": 2, "keep": False, "quote": "uno … tre", "reason": "prova abbandonata"},
            {"first": 3, "last": 4, "keep": True, "quote": "quattro … cinque", "reason": ""},
        ], "summary": "x"},
        words,
    )
    assert [(d.first, d.last, d.keep) for d in edit.decisions] == [(0, 2, False), (3, 4, True)]
    assert edit.repairs == []


def test_a_gap_in_the_answer_is_kept_not_lost():
    """The safe way to be wrong: nothing is deleted that nobody decided on."""
    words = speak(["uno due tre quattro cinque sei"])
    edit = editor.parse_edit(
        {"segments": [
            {"first": 0, "last": 1, "keep": False, "quote": "uno … due", "reason": "x"},
            {"first": 4, "last": 5, "keep": True, "quote": "cinque … sei", "reason": ""},
        ], "summary": ""},
        words,
    )
    assert edit.kept(6) == [False, False, True, True, True, True]
    assert any("non coperte" in line for line in edit.repairs)


def test_an_overlap_in_the_answer_is_trimmed_and_noted():
    words = speak(["uno due tre quattro"])
    edit = editor.parse_edit(
        {"segments": [
            {"first": 0, "last": 2, "keep": True, "quote": "uno … tre", "reason": ""},
            {"first": 1, "last": 3, "keep": False, "quote": "due … quattro", "reason": "x"},
        ], "summary": ""},
        words,
    )
    assert edit.kept(4) == [True, True, True, False]
    assert any("sovrappone" in line for line in edit.repairs)


def test_a_miscounted_index_is_corrected_from_the_quote():
    """The model says 4 and quotes «cinque sei»; «cinque» is at 4 — fine. It
    says 3 and quotes «cinque sei»: the quote wins, the numbers move."""
    words = speak(["uno due tre quattro cinque sei sette"])
    edit = editor.parse_edit(
        {"segments": [
            {"first": 0, "last": 2, "keep": True, "quote": "uno … tre", "reason": ""},
            {"first": 3, "last": 5, "keep": False, "quote": "cinque … sei", "reason": "x"},
        ], "summary": ""},
        words,
    )
    cut = next(d for d in edit.decisions if not d.keep)
    assert (cut.first, cut.last) == (4, 5)
    assert any("trovato" in line for line in edit.repairs)


# ------------------------------------------------------------------- the gates


def test_content_said_twice_is_found_even_when_the_restart_is_mid_sentence():
    """The case that put a repetition in the delivered film."""
    analysis = analysis_of(OPENING)
    kept = [True] * len(analysis.words)
    found = editor.repeated_content(analysis.words, kept)
    assert found, "la ripetizione non è stata vista"
    assert "mila euro al mese" in found[0]


def test_content_said_once_is_not_flagged():
    analysis = analysis_of(["il tuo centro è bloccato nel fango", "non riesci a vedere una via di uscita"])
    assert editor.repeated_content(analysis.words, [True] * len(analysis.words)) == []


def test_a_lexical_repeat_is_reported_not_decided():
    """«parte della frase detta bene» twice is content said twice *by the
    letter*; whether it is a refrain is the reviewer's call, so the gate
    reports it and cuts nothing."""
    analysis = analysis_of(["prima parte della frase detta bene", "seconda parte della frase detta bene"])
    kept = [True] * len(analysis.words)
    assert editor.repeated_content(analysis.words, kept)
    assert kept == [True] * len(analysis.words)


def test_a_hole_is_measured_in_the_result_not_on_set():
    """A pause the policy splits is two tenths of a second in the film: no
    hole. A pause it leaves whole — under a loose project policy — is one."""
    from skyground.analysis.cut import CutPolicy
    from skyground.analysis.models import Segment

    words = speak(["uno due", "tre quattro"], gap=1.5)
    kept = [True] * 4
    split = [Segment(0.0, 0.6, first_word=0, last_word=1), Segment(2.0, 2.6, first_word=2, last_word=3)]
    assert editor.internal_holes(words, kept, split) == []
    whole = [Segment(0.0, 2.6, first_word=0, last_word=3)]
    loose = CutPolicy(max_pause=3.0)
    assert editor.internal_holes(words, kept, whole, loose)


def test_a_clip_that_begins_after_a_cut_inside_a_sentence_is_a_candidate():
    words = speak(["uno due tre quattro. cinque sei"])
    # Cut «due tre», keep the rest: «quattro.» begins after a cut in the middle
    # of a sentence — a candidate. «cinque» after «quattro.» would not be.
    kept = [True, False, False, True, True, True]
    found = editor.mid_thought_starts(words, kept)
    assert len(found) == 1 and "quattro" in found[0]


# --------------------------------------------------------------- the whole edit


def _first_attempt_cut(analysis: Analysis) -> dict:
    words = analysis.words
    # Word indices of the second line (the good take) and the third.
    first_line = len(OPENING[0].split())
    second_line = len(OPENING[1].split())
    return {"segments": [
        {"first": 0, "last": first_line - 1, "keep": False,
         "quote": "Se … fango.", "reason": "prima ripresa dell'apertura, la seconda è più pulita"},
        {"first": first_line, "last": first_line + second_line - 1, "keep": True,
         "quote": "20 … fango.", "reason": ""},
        {"first": first_line + second_line, "last": len(words) - 1, "keep": True,
         "quote": "Non … muovi.", "reason": ""},
    ], "summary": "apertura pulita"}


def test_the_model_edits_and_the_code_realises_it():
    analysis = analysis_of(OPENING)
    model = Scripted(_first_attempt_cut(analysis), OK)

    plan = pipeline.propose(analysis, model=model)

    assert plan.status == "ready"
    first_line = len(OPENING[0].split())
    assert plan.segments[0].first_word == first_line, "il montaggio non comincia dalla ripresa buona"
    assert all(not u.kept for u in plan.utterances if u.first_word == 0)
    ok, problems = invariants.applicable(plan, analysis)
    assert ok, problems
    assert plan.editor["model"] == "finto"
    assert plan.editor["decisions"], "l'edit del modello non è sul piano"


def test_every_cut_is_a_decision_a_person_can_reverse():
    analysis = analysis_of(OPENING)
    plan = pipeline.propose(analysis, model=Scripted(_first_attempt_cut(analysis), OK))

    cuts = [q for q in plan.questions if q.id.startswith("edit:")]
    assert len(cuts) == 1
    assert cuts[0].answered_by == "motore"
    assert cuts[0].answer == "cut"
    assert "prima ripresa" in cuts[0].context
    assert any(o.id == "keep" for o in cuts[0].options)


def test_the_review_can_send_the_edit_back():
    """The model keeps both attempts; the gate sees the repeat; the review
    cuts the first. The gate's finding must reach the reviewer."""
    analysis = analysis_of(OPENING)
    first_line = len(OPENING[0].split())
    keeps_everything = {"segments": [
        {"first": 0, "last": len(analysis.words) - 1, "keep": True, "quote": "Se … muovi.", "reason": ""}
    ], "summary": ""}
    revise = {"verdict": "revise", "notes": "apertura detta due volte",
              "revisions": [{"first": 0, "last": first_line - 1, "action": "cut",
                             "reason": "ripetizione dell'apertura"}]}
    model = Scripted(keeps_everything, revise, OK)

    plan = pipeline.propose(analysis, model=model)

    review_prompt = model.asked[1][1]
    assert "detto due volte" in review_prompt, "la segnalazione automatica non è arrivata alla rilettura"
    assert plan.segments[0].first_word == first_line
    assert any("rilettura" in q.context for q in plan.questions)
    assert len(plan.editor["reviews"]) == 2


def test_answering_does_not_ask_the_model_again():
    analysis = analysis_of(OPENING)
    model = Scripted(_first_attempt_cut(analysis), OK)
    plan = pipeline.propose(analysis, model=model)
    question = next(q for q in plan.questions if q.id.startswith("edit:"))

    # The scripted model has no answers left: any further question would fail.
    restored = pipeline.answer(analysis, plan, question.id, "keep", answered_by="gabriele@skyground.online", model=model)

    assert restored.segments[0].first_word == 0, "la risposta non ha rimesso il pezzo"
    kept = next(q for q in restored.questions if q.id == question.id)
    assert kept.answer == "keep"
    assert kept.answered_by == "gabriele@skyground.online"
    assert restored.editor["decisions"] == plan.editor["decisions"]


def test_a_persons_answer_survives_a_regenerate():
    analysis = analysis_of(OPENING)
    plan = pipeline.propose(analysis, model=Scripted(_first_attempt_cut(analysis), OK))
    question = next(q for q in plan.questions if q.id.startswith("edit:"))
    answered = pipeline.answer(analysis, plan, question.id, "keep", answered_by="g@x")

    again = pipeline.propose(analysis, model=Scripted(_first_attempt_cut(analysis), OK),
                             decisions=pipeline.decisions_from(answered))

    assert again.segments[0].first_word == 0


def test_a_refusal_or_truncation_is_an_error_not_a_silent_fallback():
    class Refuses:
        name = "rifiuta"

        def ask(self, *_):
            from skyground.errors import ConfigurationError
            raise ConfigurationError("il modello ha rifiutato")

    from skyground.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        pipeline.propose(analysis_of(OPENING), model=Refuses())


def test_the_plan_round_trips_through_json():
    analysis = analysis_of(OPENING)
    plan = pipeline.propose(analysis, model=Scripted(_first_attempt_cut(analysis), OK))
    again = CutPlan.from_dict(plan.as_dict())
    assert again.editor == plan.editor
    assert [s.first_word for s in again.segments] == [s.first_word for s in plan.segments]


def test_the_editor_policy_leaves_a_breath_not_a_hole():
    """The old policy padded every join with 0.40s of air; a person said the
    film had holes in it. A join is now a fifth of a second."""
    assert editor.EDITOR_POLICY.lead_in + editor.EDITOR_POLICY.lead_out <= 0.25
    assert editor.EDITOR_POLICY.max_pause <= editor.HOLE
