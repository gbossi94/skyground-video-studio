"""The cut engine: the invariants, and the refusal to guess.

The fixtures here are synthetic on purpose — they reproduce the *shapes* found
in the real footage (a restarted sentence, a reformulation, a rhetorical pause,
a hesitation) without carrying a client's outtakes into the repository. The
engine is additionally validated against the real 6-minute take; that test runs
when the media is available and skips otherwise.
"""

from __future__ import annotations

import itertools

import pytest

from skyground.analysis import invariants, pipeline
from skyground.analysis.adviser import NullAdviser
from skyground.analysis.cut import CutPolicy, plan_cut
from skyground.analysis.models import (
    ASK_PAUSE_INTENT,
    ASK_TAKE_CHOICE,
    REASON_RETAKE,
    REASON_SILENCE,
    Analysis,
    Word,
)
from skyground.analysis.takes import build_utterances, group_takes, similarity
from skyground.errors import ValidationError


def speak(start: float, text: str, *, pace: float = 0.32, gap: float = 0.06, p: float = 0.95):
    """Lay a sentence out on a timeline, one word after another."""
    words, cursor = [], start
    for token in text.split():
        words.append(Word(t=round(cursor, 3), end=round(cursor + pace, 3), s=token, p=p))
        cursor += pace + gap
    return words, cursor - gap


def analysis_of(*groups, duration: float | None = None) -> Analysis:
    words = list(itertools.chain.from_iterable(groups))
    end = max(word.end for word in words)
    return Analysis(source="raw.mov", duration=duration or end + 2.0, words=words)


@pytest.fixture
def restarted_sentence() -> Analysis:
    """The commonest shape: the speaker begins, stops, and begins again."""
    first, end = speak(3.0, "Se il tuo centro è bloccato sei nel fango.")
    second, _ = speak(end + 2.5, "Se il tuo centro è bloccato sei in quello che chiamo fango.")
    return analysis_of(first, second)


# ------------------------------------------------------------------ invariants


def test_a_plan_of_the_real_shape_breaks_no_rule(restarted_sentence):
    plan = plan_cut(restarted_sentence)
    assert invariants.check(plan, restarted_sentence) == []


@pytest.mark.parametrize("lead_in", [0.0, 0.12, 0.4, 1.5])
@pytest.mark.parametrize("max_pause", [0.2, 0.6, 2.0])
def test_no_cut_ever_lands_inside_a_word(restarted_sentence, lead_in, max_pause):
    """The rule that protects the voice, swept across policies that stress it.

    A generous lead-in wants to reach backwards into the previous word and a
    tiny `max_pause` wants to cut between every syllable; neither may.
    """
    policy = CutPolicy(lead_in=lead_in, lead_out=lead_in, max_pause=max_pause)
    plan = plan_cut(restarted_sentence, policy)
    offending = [
        item for item in invariants.check(plan, restarted_sentence, min_segment=policy.min_segment)
        if item.rule in ("taglio-dentro-una-parola", "parola-spezzata")
    ]
    assert offending == []


def test_every_instant_of_the_source_is_accounted_for(restarted_sentence):
    """Kept plus removed is the whole take: nothing vanishes unrecorded."""
    plan = plan_cut(restarted_sentence)
    covered = sum(segment.duration for segment in plan.segments)
    covered += sum(item.duration for item in plan.removed)
    assert covered == pytest.approx(restarted_sentence.duration, abs=0.01)
    assert [item for item in invariants.check(plan, restarted_sentence)
            if item.rule == "regione-non-classificata"] == []


def test_every_removal_states_a_reason(restarted_sentence):
    plan = plan_cut(restarted_sentence)
    assert plan.removed
    assert all(item.reason and item.detail for item in plan.removed)


def test_segments_never_overlap_and_run_forward(restarted_sentence):
    plan = plan_cut(restarted_sentence)
    for before, after in zip(plan.segments, plan.segments[1:], strict=False):
        assert before.end <= after.start + 1e-6
        assert before.duration > 0


def test_a_hand_written_bad_plan_is_caught():
    """The checker must fail a plan that the engine would never produce."""
    words, _ = speak(1.0, "una frase qualsiasi da tagliare male")
    analysis = analysis_of(words)
    from skyground.analysis.models import CutPlan, Removed, Segment

    middle = words[2].t + (words[2].end - words[2].t) / 2
    broken = CutPlan(
        "raw.mov",
        analysis.duration,
        segments=[Segment(0.5, middle)],
        removed=[Removed(middle, analysis.duration, REASON_SILENCE, 1.0, "x")],
    )
    rules = {item.rule for item in invariants.check(broken, analysis)}
    assert "taglio-dentro-una-parola" in rules
    assert "parola-spezzata" in rules


# ----------------------------------------------------------------- the pauses


def test_a_long_pause_is_trimmed_not_kept_whole():
    first, end = speak(1.0, "prima parte della frase.")
    second, _ = speak(end + 3.0, "seconda parte della frase.")
    analysis = analysis_of(first, second)
    plan = plan_cut(analysis, CutPolicy(keep_pause=0.2))

    pause = next(item for item in plan.removed if item.reason == REASON_SILENCE)
    assert pause.duration > 2.0
    assert plan.output_duration < analysis.duration


def test_a_mid_sentence_pause_is_asked_about_not_assumed():
    """No full stop before the silence: it may be a beat the speaker wanted.

    Only when somebody asked to be asked: the default policy calls no pause
    deliberate by its length any more, because the hand edit it is measured
    against keeps none longer than a quarter of a second.
    """
    first, end = speak(1.0, "e quindi ti invito a fare")
    second, _ = speak(end + 1.6, "una scommessa seria questa volta.")
    analysis = analysis_of(first, second)
    plan = plan_cut(analysis, CutPolicy(ask_when_unsure=True, rhetorical_pause=1.20))

    asked = [item for item in plan.questions if item.kind == ASK_PAUSE_INTENT]
    assert len(asked) == 1
    assert plan.status == "draft"


def test_keeping_a_rhetorical_pause_leaves_it_in_the_edit():
    first, end = speak(1.0, "e quindi ti invito a fare")
    second, _ = speak(end + 1.6, "una scommessa seria questa volta.")
    analysis = analysis_of(first, second)

    policy = CutPolicy(rhetorical_pause=1.20)
    asked = next(q for q in plan_cut(analysis, policy).questions if q.kind == ASK_PAUSE_INTENT)
    kept = plan_cut(analysis, policy, decisions={asked.id: "keep"})
    trimmed = plan_cut(analysis, policy, decisions={asked.id: "cut"})
    assert kept.output_duration > trimmed.output_duration


def test_the_edges_follow_the_measured_silence_not_the_transcript():
    """What makes two pieces sound joined is how much silence is left between
    them. The transcript's word edges are approximate — trimming to them once
    clipped the tails of words — so where the waveform reported its own
    silence, that is what the cut is placed against.

    Measured on the hand edit of the reference footage: its joins sit at
    0.19s of silence, and no pause in it runs past 0.25s.
    """
    from skyground.analysis.models import Silence

    first, end = speak(1.0, "prima frase detta bene.")
    second, _ = speak(end + 1.4, "seconda frase, altrettanto.")
    analysis = analysis_of(first, second)
    last_word = first[-1]
    # The voice really stops a tenth after the transcript says it does, and
    # starts again a tenth before the next word.
    quiet = Silence(start=last_word.end + 0.10, end=second[0].t - 0.10)
    measured = Analysis(
        source=analysis.source, duration=analysis.duration,
        words=analysis.words, silences=[quiet],
    )

    plan = plan_cut(measured)
    join = next(seg for seg in plan.segments if seg.last_word == len(first) - 1)
    after = next(seg for seg in plan.segments if seg.first_word == len(first))

    assert join.end == pytest.approx(quiet.start + CutPolicy().tail_air, abs=0.01)
    assert join.end > last_word.end, "una coda tagliata è il difetto da non rifare"
    assert after.start == pytest.approx(quiet.end - CutPolicy().head_air, abs=0.01)
    # The pause itself is cut out; what is heard at the join is the air left
    # on either side of it, which is the number the ear judges.
    heard = (join.end - quiet.start) + (quiet.end - after.start)
    assert heard == pytest.approx(0.20, abs=0.02)


# ------------------------------------------------------------------- the takes


def test_a_restarted_sentence_is_recognised(restarted_sentence):
    utterances = build_utterances(restarted_sentence.words)
    groups = group_takes(utterances)
    assert len(groups) == 1
    assert len(groups[0].utterances) == 2


def test_similarity_separates_a_retake_from_a_different_line():
    assert similarity("se il tuo centro è bloccato", "se il tuo centro è fermo") > 0.6
    assert similarity("prenota una call gratuita", "il fango non ti fa muovere") < 0.3


def test_an_echo_far_away_is_not_treated_as_a_retake():
    """The same phrase two minutes later is rhetoric, not a mistake."""
    first, _ = speak(1.0, "devi fare una scommessa seria.")
    second, _ = speak(200.0, "devi fare una scommessa seria.")
    analysis = analysis_of(first, second, duration=210.0)
    groups = group_takes(build_utterances(analysis.words), window_seconds=120.0)
    assert groups == []


def test_a_close_call_between_takes_becomes_a_question(restarted_sentence):
    """Two good attempts at the same line: the engine must not pick one."""
    plan = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True))
    question = next(item for item in plan.questions if item.kind == ASK_TAKE_CHOICE)
    assert plan.status == "draft"
    # Nothing was thrown away while the question is open.
    assert not [item for item in plan.removed if item.reason == REASON_RETAKE]
    assert len(question.options) == 3  # two takes plus "keep both"
    assert sum(1 for option in question.options if option.recommended) == 1


def test_a_clear_winner_is_decided_without_asking():
    """One attempt trails off unfinished; that is a fact, not a preference.

    The decision is still written down — every one of them is — but it does not
    stop the plan from being applied.
    """
    aborted, end = speak(1.0, "se il tuo centro è")
    complete, _ = speak(end + 2.0, "se il tuo centro è bloccato sei nel fango.")
    analysis = analysis_of(aborted, complete)
    plan = plan_cut(analysis, CutPolicy(decide_margin=0.05))

    assert not [item for item in plan.questions if not item.resolved]
    assert [item for item in plan.removed if item.reason == REASON_RETAKE]


def test_answering_a_take_question_honours_the_answer(restarted_sentence):
    """Whichever attempt the answer names is the one left standing."""
    policy = CutPolicy(decide_margin=0.9, ask_when_unsure=True)
    plan = plan_cut(restarted_sentence, policy)
    question = next(item for item in plan.questions if item.kind == ASK_TAKE_CHOICE)

    resolved = plan_cut(restarted_sentence, policy, {question.id: "first"})

    assert resolved.utterances[0].kept is True
    assert any(not utterance.kept for utterance in resolved.utterances)


def test_keeping_both_takes_removes_neither(restarted_sentence):
    plan = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True))
    question = next(item for item in plan.questions if item.kind == ASK_TAKE_CHOICE)
    resolved = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True), {question.id: "keep-both"})
    assert all(utterance.kept for utterance in resolved.utterances)


# ------------------------------------------------------- refusing to be applied


def test_a_plan_with_an_open_question_is_not_applicable(restarted_sentence):
    plan = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True))
    ok, problems = invariants.applicable(plan, restarted_sentence)
    assert ok is False
    assert any("domanda aperta" in problem for problem in problems)


def test_applying_an_undecided_plan_is_refused(restarted_sentence):
    plan = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True))
    with pytest.raises(ValidationError) as error:
        pipeline.apply_to_timeline(plan, restarted_sentence, {"source": "assets/raw.mov"})
    assert "non è applicabile" in str(error.value)


def test_an_answered_plan_becomes_a_timeline(restarted_sentence):
    plan = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True))
    current = plan
    while current.open_questions:
        question = current.open_questions[0]
        current = pipeline.answer(
            restarted_sentence, current, question.id, question.options[0].id, answered_by="tester"
        )
    assert current.status == "ready"

    timeline = pipeline.apply_to_timeline(current, restarted_sentence, {"source": "assets/raw.mov"})
    assert timeline["clips"]
    assert timeline["duration"] == pytest.approx(current.output_duration, abs=0.001)
    # The shape `timeline.json` already uses: output_start runs consecutively.
    cursor = 0.0
    for clip in timeline["clips"]:
        assert clip["output_start"] == pytest.approx(cursor, abs=0.002)
        cursor += clip["end"] - clip["start"]


def test_an_answer_is_remembered_after_the_rebuild(restarted_sentence):
    plan = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True))
    question = plan.open_questions[0]
    rebuilt = pipeline.answer(
        restarted_sentence, plan, question.id, question.options[0].id, answered_by="gabriele"
    )
    recorded = next(item for item in rebuilt.questions if item.id == question.id)
    assert recorded.answer == question.options[0].id
    assert recorded.answered_by == "gabriele"
    assert pipeline.decisions_from(rebuilt) == {question.id: question.options[0].id}


def test_an_unknown_answer_is_refused(restarted_sentence):
    plan = plan_cut(restarted_sentence, CutPolicy(decide_margin=0.9, ask_when_unsure=True))
    question = plan.open_questions[0]
    with pytest.raises(ValidationError):
        pipeline.answer(restarted_sentence, plan, question.id, "utterance:999")
    with pytest.raises(ValidationError):
        pipeline.answer(restarted_sentence, plan, "take:non-esiste", "keep-both")


# --------------------------------------------------------------- the adviser


class FakeAdviser:
    name = "fake"

    def __init__(self, pairs):
        self.pairs = pairs

    def suspects(self, utterances):
        return self.pairs


def test_an_adviser_raises_candidates_and_the_engine_decides_them():
    """A reformulation the word comparison cannot see, found by meaning.

    The adviser proposes; it never edits. What happens to its proposal is the
    engine's call, taken and written down — the same treatment every other close
    call gets since the engine stopped handing its uncertainty to a person.
    """
    first, end = speak(1.0, "il primo passo è una call conoscitiva.")
    second, _ = speak(end + 2.0, "parliamone mezz'ora insieme senza impegno.")
    analysis = analysis_of(first, second)

    plain = plan_cut(analysis)
    assert not plain.questions

    advised = pipeline.propose(
        analysis, adviser=FakeAdviser([{"a": 0, "b": 1, "why": "stesso invito"}])
    )
    assert len(advised.questions) == 1
    recorded = advised.questions[0]
    assert recorded.resolved is True
    assert recorded.answered_by == "motore"
    assert advised.status == "ready"


def test_an_adviser_candidate_still_blocks_when_asked_to(restarted_sentence):
    """The old behaviour is a setting, not a thing that was deleted."""
    first, end = speak(1.0, "il primo passo è una call conoscitiva.")
    second, _ = speak(end + 2.0, "parliamone mezz'ora insieme senza impegno.")
    analysis = analysis_of(first, second)

    advised = plan_cut(
        analysis,
        CutPolicy(ask_when_unsure=True),
        suspects=[{"a": 0, "b": 1, "why": "stesso invito"}],
    )
    assert advised.status == "draft"
    assert advised.open_questions
    # Nothing left the edit while the question stands.
    assert all(utterance.kept for utterance in advised.utterances)


def test_a_broken_adviser_costs_nothing():
    """Nonsense indices must not reach the plan."""
    from skyground.analysis.adviser import ClaudeAdviser

    adviser = ClaudeAdviser("chiave")
    assert adviser._clean('{"pairs": [{"a": 0, "b": 500, "why": "x"}]}', 3) == []
    assert adviser._clean("non è json", 3) == []
    assert adviser._clean('{"pairs": [{"a": 2, "b": 0, "why": "y"}]}', 3) == [
        {"a": 0, "b": 2, "why": "y"}
    ]


def test_the_default_adviser_changes_nothing(restarted_sentence):
    assert (
        pipeline.propose(restarted_sentence, adviser=NullAdviser()).as_dict()
        == pipeline.propose(restarted_sentence).as_dict()
    )


# ------------------------------------------------------------- reproducibility


def test_the_same_inputs_always_give_the_same_plan(restarted_sentence):
    """An approved edit must be explainable months later."""
    first = plan_cut(restarted_sentence, now="2026-01-01T00:00:00Z")
    second = plan_cut(restarted_sentence, now="2026-01-01T00:00:00Z")
    assert first.as_dict() == second.as_dict()


def test_a_plan_survives_a_round_trip_through_json(restarted_sentence):
    from skyground.analysis.models import CutPlan

    plan = plan_cut(restarted_sentence, now="2026-01-01T00:00:00Z")
    assert CutPlan.from_dict(plan.as_dict()).as_dict() == plan.as_dict()


def test_silence_only_footage_produces_no_segments():
    analysis = Analysis(source="raw.mov", duration=30.0, words=[])
    plan = plan_cut(analysis)
    assert plan.segments == []
    assert plan.removed[0].duration == pytest.approx(30.0)
    assert invariants.check(plan, analysis) == []


# ---------------------------------------------- decisions have to stay visible


def test_a_decision_stays_in_the_plan_after_a_rebuild(restarted_sentence):
    """Answering used to consume the question: the rebuilt plan carried the
    effect of the choice with no trace of the choice itself, so nobody could see
    what had been decided — or change their mind. Found in production, on a plan
    that had quietly gone from six open questions to four."""
    first = plan_cut(restarted_sentence)
    question = next(q for q in first.questions if q.kind == ASK_TAKE_CHOICE)
    chosen = question.options[0].id

    rebuilt = plan_cut(restarted_sentence, None, {question.id: chosen})

    kept = next((q for q in rebuilt.questions if q.id == question.id), None)
    assert kept is not None, "la domanda decisa è sparita dal piano"
    assert kept.answer == chosen
    assert kept.resolved is True
    # And it no longer blocks: a decision taken is not an ambiguity.
    assert [q for q in rebuilt.questions if not q.resolved] != first.questions


def test_rebuilding_does_not_quietly_lose_answers(restarted_sentence):
    """The exact sequence that lost them: answer, then regenerate."""
    plan = plan_cut(restarted_sentence)
    question = next(q for q in plan.questions if q.kind == ASK_TAKE_CHOICE)
    answered = pipeline.answer(restarted_sentence, plan, question.id, question.options[0].id)
    assert sum(1 for q in answered.questions if q.resolved) == 1

    again = plan_cut(restarted_sentence, None, pipeline.decisions_from(answered))
    assert sum(1 for q in again.questions if q.resolved) == 1
    assert len(again.questions) == len(answered.questions)


def test_keeping_both_takes_is_also_a_decision_on_the_record(restarted_sentence):
    plan = plan_cut(restarted_sentence)
    question = next(q for q in plan.questions if q.kind == ASK_TAKE_CHOICE)
    rebuilt = plan_cut(restarted_sentence, None, {question.id: "keep-both"})
    kept = next(q for q in rebuilt.questions if q.id == question.id)
    assert kept.answer == "keep-both"
    assert invariants.check(rebuilt, restarted_sentence) == []


# --------------------------------------- what the recommendation is worth


def test_a_take_that_trails_off_is_never_the_recommended_one():
    """A transcriber writes «…» exactly where the speaker broke off. Reading it
    as a finished sentence inverted the signal: the engine recommended the
    abandoned attempt, and following its advice cost a quarter of the approved
    edit on the reference footage."""
    from skyground.analysis.takes import build_utterances, score_take

    words = [
        Word(t=i * 0.4, end=i * 0.4 + 0.3, s=text, p=0.95)
        for i, text in enumerate("e poi in ogni caso non devi…".split())
    ] + [
        Word(t=4.0 + i * 0.4, end=4.0 + i * 0.4 + 0.3, s=text, p=0.95)
        for i, text in enumerate("e poi in ogni caso non è proprio un salto nel vuoto.".split())
    ]
    utterances = build_utterances(words, gap=0.55)
    assert len(utterances) == 2

    abandoned, delivered = (
        score_take(utterances[0], words, utterances, 0),
        score_take(utterances[1], words, utterances, 1),
    )
    assert abandoned.complete == 0.0
    assert delivered.total > abandoned.total


def test_a_tidy_fragment_does_not_beat_the_sentence_it_came_from():
    """«ancora alzando i prezzi.» ends in a full stop and says almost nothing.
    It used to out-score the take that delivered the thought."""
    from skyground.analysis.takes import build_utterances, score_take

    long_take = "E magari questo vuol dire rinunciare a un po' di estetica di base o alzare i prezzi"
    words = [
        Word(t=i * 0.4, end=i * 0.4 + 0.3, s=text, p=0.95)
        for i, text in enumerate(long_take.split())
    ] + [
        Word(t=20.0 + i * 0.4, end=20.0 + i * 0.4 + 0.3, s=text, p=0.95)
        for i, text in enumerate("ancora alzando i prezzi.".split())
    ]
    utterances = build_utterances(words, gap=0.55)
    full, fragment = (
        score_take(utterances[0], words, utterances, 0),
        score_take(utterances[1], words, utterances, 1),
    )
    assert full.total > fragment.total


def test_a_suspected_reformulation_is_not_decided_by_position(restarted_sentence):
    """The recommendation on an adviser's pair used to be «the second one»,
    always — a rule this codebase documents as wrong, since on the reference
    footage the editor kept the first attempt of the opening line."""
    plan = plan_cut(restarted_sentence, suspects=[{"a": 0, "b": 1, "why": "prova"}])
    question = next(q for q in plan.questions if q.id == "take:0-1")

    marked = [option for option in question.options if option.recommended]
    assert len(marked) <= 1
    if marked:
        # Whatever it recommends, it is not "the later one because it is later".
        assert marked[0].id in {"utterance:0", "utterance:1"}
        scored = {option.id: option for option in question.options}
        assert scored["utterance:0"].start is not None
        assert scored["utterance:1"].start is not None


def test_two_takes_too_close_to_separate_get_no_recommendation():
    """Marking one anyway invites a person to click through a choice the engine
    cannot actually make. Two different wordings of the same thought: the
    adviser pairs them, the score cannot separate them."""
    first = "il fatturato resta fermo ogni singolo mese dell anno"
    second = "i ricavi non salgono mai in nessun mese dell anno"
    words = [
        Word(t=i * 0.4, end=i * 0.4 + 0.3, s=text, p=0.95)
        for i, text in enumerate(first.split())
    ] + [
        Word(t=10.0 + i * 0.4, end=10.0 + i * 0.4 + 0.3, s=text, p=0.95)
        for i, text in enumerate(second.split())
    ]
    analysis = Analysis(source="raw.mov", duration=16.0, words=words)

    plan = plan_cut(analysis, suspects=[{"a": 0, "b": 1, "why": "stessa idea"}])

    question = next(q for q in plan.questions if q.id == "take:0-1")
    assert [o for o in question.options if o.recommended] == []


# ------------------------------------------ tornare indietro da una scelta


def test_a_rebuild_keeps_what_a_person_chose():
    """Overruling somebody's choice because they pressed regenerate would be
    the worst kind of surprise."""
    from skyground.analysis import pipeline

    aborted, end = speak(1.0, "se il tuo centro è bloccato")
    complete, _ = speak(end + 2.0, "se il tuo centro è bloccato sei nel fango.")
    analysis = analysis_of(aborted, complete)

    plan = plan_cut(analysis)
    question = next(item for item in plan.questions if item.resolved)
    against_the_engine = next(
        option.id for option in question.options if option.id != question.answer
    )
    chosen = pipeline.answer(
        analysis, plan, question.id, against_the_engine, answered_by="gabriele@skyground.online"
    )

    again = pipeline.propose(analysis, decisions=pipeline.decisions_from(chosen))

    kept = next(item for item in again.questions if item.id == question.id)
    assert kept.answer == against_the_engine


def test_starting_from_scratch_gives_the_engine_the_last_word_again():
    """The only way back from a choice that turned out to be wrong."""
    from skyground.analysis import pipeline

    aborted, end = speak(1.0, "se il tuo centro è bloccato")
    complete, _ = speak(end + 2.0, "se il tuo centro è bloccato sei nel fango.")
    analysis = analysis_of(aborted, complete)

    plan = plan_cut(analysis)
    question = next(item for item in plan.questions if item.resolved)
    engine_choice = question.answer
    against = next(option.id for option in question.options if option.id != engine_choice)
    pipeline.answer(analysis, plan, question.id, against, answered_by="gabriele@skyground.online")

    # No decisions carried over at all: propose from the material alone.
    fresh = pipeline.propose(analysis)

    restored = next(item for item in fresh.questions if item.id == question.id)
    assert restored.answer == engine_choice
    assert restored.answered_by == "motore"
