"""The hand edit: the cut as a person left it on the timeline.

Pure tests over the engine's own fixtures — the shapes of the real footage,
none of its words. What is under test is that a person's kept ranges become a
plan that breaks no rule, that whatever the client sends a boundary never lands
in speech, and that the engine's reasoning survives the correction instead of
being overwritten by it.
"""

from __future__ import annotations

import pytest

from skyground.analysis import invariants, manual
from skyground.analysis.cut import ENGINE, CutPolicy, plan_cut
from skyground.analysis.models import (
    ASK_TAKE_CHOICE,
    REASON_MANUAL,
    REASON_RETAKE,
    REASON_SILENCE,
    Option,
    Question,
)
from skyground.errors import ValidationError
from tests.test_cut_engine import analysis_of, speak

POLICY = CutPolicy()


@pytest.fixture
def restarted():
    """The commonest shape: the speaker begins, stops, and begins again."""
    first, end = speak(3.0, "Se il tuo centro è bloccato sei nel fango.")
    second, end = speak(end + 2.5, "Se il tuo centro è bloccato sei in quello che chiamo fango.")
    third, _ = speak(end + 1.8, "Non importa quanto premi non ti muovi.")
    return analysis_of(first, second, third)


def realised(analysis, kept, previous=None, policy=POLICY, **kwargs):
    previous = previous or plan_cut(analysis, POLICY)
    return manual.realise(analysis, previous, kept, policy=policy, edited_by="gabriele", **kwargs)


# ---------------------------------------------------------------- identity


def test_the_engine_plan_read_back_and_realised_is_the_same_plan(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    again = realised(restarted, kept, plan)
    assert [(s.start, s.end, s.first_word, s.last_word) for s in again.segments] == [
        (s.start, s.end, s.first_word, s.last_word) for s in plan.segments
    ]
    assert [(r.start, r.end, r.reason) for r in again.removed] == [
        (r.start, r.end, r.reason) for r in plan.removed
    ]
    assert again.manual["editedBy"] == "gabriele"
    assert again.manual["engineKept"] == [[s.first_word, s.last_word] for s in plan.segments]
    assert again.status == "ready"
    assert invariants.check(again, restarted) == []


# ------------------------------------------------------------- boundaries


def test_a_boundary_dragged_into_a_word_stops_at_the_edge_of_the_word(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    first = kept[0]
    inside_first_word = restarted.words[first.first].t + 0.1
    moved = [manual.Kept(first.first, first.last, inside_first_word, first.end)] + kept[1:]
    again = realised(restarted, moved, plan)
    assert again.segments[0].start == pytest.approx(restarted.words[first.first].t, abs=0.002)
    assert invariants.check(again, restarted) == []


def test_a_boundary_dragged_into_the_gap_stays_where_it_was_put(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    last = kept[-1]
    # Halfway through the dead air after the last word.
    into_the_air = restarted.words[last.last].end + 1.0
    moved = kept[:-1] + [manual.Kept(last.first, last.last, last.start, into_the_air)]
    again = realised(restarted, moved, plan)
    assert again.segments[-1].end == pytest.approx(into_the_air, abs=0.002)
    assert again.removed[-1].reason == "lead-out"
    assert invariants.check(again, restarted) == []


def test_a_boundary_dragged_back_over_the_previous_piece_joins_the_two(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    last = kept[-1]
    before_the_previous_word = restarted.words[last.first - 1].t - 0.5
    moved = kept[:-1] + [manual.Kept(last.first, last.last, before_the_previous_word, last.end)]
    again = realised(restarted, moved, plan)
    # Clamped to where the previous piece ends, so the two touch and become one:
    # the pause between them is gone, and no cut is anywhere near a word.
    assert len(again.segments) == len(plan.segments) - 1
    assert again.segments[-1].last_word == last.last
    assert invariants.check(again, restarted) == []


# ---------------------------------------------------------------- removing


def test_taking_out_words_the_engine_kept_is_recorded_as_a_manual_removal(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    # Drop the last kept range altogether.
    again = realised(restarted, kept[:-1], plan)
    gone = kept[-1]
    said = " ".join(w.s for w in restarted.words[gone.first : gone.last + 1])
    manual_removals = [r for r in again.removed if r.reason == REASON_MANUAL]
    assert len(manual_removals) == 1
    assert said[:30] in manual_removals[0].detail
    assert not again.utterances[-1].kept
    assert again.utterances[-1].drop_reason == "tolto a mano"
    assert invariants.check(again, restarted) == []


def test_what_the_engine_removed_keeps_the_engine_reason(restarted):
    plan = plan_cut(restarted, POLICY)
    retakes = [r for r in plan.removed if r.reason == REASON_RETAKE]
    assert retakes, "the fixture has a restarted sentence the engine removes"
    kept = manual.from_plan(plan, restarted.words)
    again = realised(restarted, kept, plan)
    assert [r.detail for r in again.removed if r.reason == REASON_RETAKE] == [
        r.detail for r in retakes
    ]


def test_a_split_inside_continuous_speech_joins_back_and_a_pause_stays_a_silence(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    # Splitting between two words with no pause between them changes nothing:
    # the two halves touch and are one segment again. A split is a cut only
    # once something is removed between the halves.
    last = kept[-1]
    middle = last.first + 3
    split = kept[:-1] + [
        manual.Kept(last.first, middle, last.start, None),
        manual.Kept(middle + 1, last.last, None, last.end),
    ]
    again = realised(restarted, split, plan)
    assert len(again.segments) == len(plan.segments)
    # The breath between the two kept sentences is still a silence, measured.
    silence = [r for r in again.removed if r.reason == REASON_SILENCE]
    assert silence and silence[0].detail.startswith("pausa di")
    assert invariants.check(again, restarted) == []


# ------------------------------------------------------------------ refusals


def test_a_piece_shorter_than_the_policy_minimum_is_refused_not_altered(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    last = kept[-1]
    tiny = kept[:-1] + [manual.Kept(last.first, last.first, None, None)]
    with pytest.raises(ValidationError) as caught:
        realised(restarted, tiny, plan, policy=CutPolicy(min_segment=0.8))
    assert "minimo 0.80s" in str(caught.value)
    # Under the default policy one padded word is long enough, and stays.
    assert realised(restarted, tiny, plan).segments[-1].last_word == last.first


def test_ranges_out_of_order_or_overlapping_are_refused(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    with pytest.raises(ValidationError):
        realised(restarted, list(reversed(kept)), plan)
    overlapping = [manual.Kept(kept[0].first, kept[0].last + 1), manual.Kept(kept[0].last, kept[0].last + 5)]
    with pytest.raises(ValidationError):
        realised(restarted, overlapping, plan)
    with pytest.raises(ValidationError):
        realised(restarted, [manual.Kept(0, len(restarted.words))], plan)


# ------------------------------------------------------------- questions


def test_putting_back_a_piece_the_engine_cut_flips_its_question_to_the_person(restarted):
    plan = plan_cut(restarted, POLICY)
    kept = manual.from_plan(plan, restarted.words)
    # The engine (as the model does) recorded a cut as a question answered by itself.
    gone = [r for r in plan.removed if r.reason == REASON_RETAKE][0]
    inside = [i for i, w in enumerate(restarted.words) if w.t >= gone.start and w.end <= gone.end]
    first, last = inside[0], inside[-1]
    plan.questions.append(
        Question(
            id=f"edit:{first}-{last}", kind=ASK_TAKE_CHOICE, at=gone.start, prompt="Questo pezzo va tolto?",
            options=[Option("cut", "Toglierlo"), Option("keep", "Tenerlo")], answer="cut", answered_by=ENGINE,
        )
    )
    reinstated = manual.with_answer(kept, first, last, keep=True)
    again = realised(restarted, reinstated, plan)
    question = next(q for q in again.questions if q.id == f"edit:{first}-{last}")
    assert question.answer == "keep"
    assert question.answered_by == manual.MANUAL
    assert not any(r.reason == REASON_RETAKE for r in again.removed)
    assert invariants.check(again, restarted) == []


def test_with_answer_cut_splits_a_range_and_keep_merges_neighbours():
    kept = [manual.Kept(0, 10, 1.0, 5.0), manual.Kept(20, 30)]
    cut = manual.with_answer(kept, 3, 5, keep=False)
    assert [(k.first, k.last) for k in cut] == [(0, 2), (6, 10), (20, 30)]
    assert cut[0].start == 1.0 and cut[0].end is None
    assert cut[1].start is None and cut[1].end == 5.0
    back = manual.with_answer(cut, 3, 5, keep=True)
    assert [(k.first, k.last) for k in back] == [(0, 10), (20, 30)]


def test_a_manual_layer_makes_the_plan_applicable_whatever_the_engine_left_open(restarted):
    plan = plan_cut(restarted, CutPolicy(ask_when_unsure=True))
    kept = manual.from_plan(plan, restarted.words)
    again = realised(restarted, kept, plan)
    assert again.status == "ready"
    ok, problems = invariants.applicable(again, restarted)
    assert ok, problems


# ------------------------------------------------------------- the timeline


def test_a_timeline_becomes_kept_ranges_and_boundaries_inside_words_are_noted(restarted):
    plan = plan_cut(restarted, POLICY)
    clips = plan.to_timeline_clips()
    kept, notes = manual.from_timeline(restarted, clips)
    assert notes == []
    assert [(k.first, k.last) for k in kept] == [(s.first_word, s.last_word) for s in plan.segments]

    # A clip whose end was written inside a word, by hand.
    word = restarted.words[plan.segments[0].last_word]
    clips[0]["end"] = word.t + 0.05
    kept, notes = manual.from_timeline(restarted, clips)
    assert any("dentro" in note for note in notes)
    again = realised(restarted, kept, plan)
    assert invariants.check(again, restarted) == []


def test_a_clip_with_no_whole_word_is_skipped_with_a_note(restarted):
    plan = plan_cut(restarted, POLICY)
    clips = plan.to_timeline_clips()
    clips.insert(0, {"start": 0.0, "end": 0.5})
    kept, notes = manual.from_timeline(restarted, clips)
    assert len(kept) == len(plan.segments)
    assert notes and "senza parole" in notes[0]
