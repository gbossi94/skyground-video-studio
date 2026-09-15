"""Two blind spots the reference footage exposed, and the numbers that found them.

For a day the engine was believed to be roughly right. Measured against the edit
a person actually approved it kept 99.5% of the right material — and proposed
more than twice as much, because of two things it could not see:

* Whisper stretches a word across the pause that follows it, so the gaps the
  engine looks for were not there to be found;
* a speaker stumbles inside one breath far more often than they re-shoot a line,
  and comparing whole utterances to each other cannot see that at all.

These tests pin both, and the harness that measures them.
"""

from __future__ import annotations

from skyground.analysis import align, evaluation, takes
from skyground.analysis.cut import plan_cut
from skyground.analysis.models import Analysis, Silence, Word


def words_from(script: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(t=start, end=end, s=text, p=0.95) for text, start, end in script]


# ------------------------------------------------------- trusting the audio


def test_a_word_stretched_over_a_pause_is_trimmed_at_the_silence():
    """The exact shape found in the footage: one word reported as lasting 11
    seconds because the speaker stopped talking in the middle of it."""
    words = words_from([("stai", 1.0, 12.6), ("letteralmente", 12.6, 13.0)])
    silences = [Silence(3.4, 12.0)]

    corrected = align.clamp_words(words, silences)

    assert corrected[0].end == 3.4
    assert corrected[1].t == 12.6  # untouched: it sits outside the silence
    # And now the gap between them is visible to anything looking for one.
    assert corrected[1].t - corrected[0].end > 9


def test_a_word_wholly_inside_a_silence_is_left_alone():
    """The transcriber heard something there. Deleting it would be the engine
    overruling the words that were said, which is not its job."""
    words = words_from([("mh", 5.0, 5.3)])
    corrected = align.clamp_words(words, [Silence(4.0, 6.0)])
    assert (corrected[0].t, corrected[0].end) == (5.0, 5.3)


def test_without_silences_nothing_is_touched():
    words = words_from([("uno", 0.0, 0.4), ("due", 0.4, 0.9)])
    assert align.clamp_words(words, []) == words


def test_the_correction_reaches_the_segments():
    """It did not, the first time: `_build_segments` read the original words
    again and the fix did nothing at all. The measured precision was what
    revealed it — not a green test."""
    words = words_from([("stai", 1.0, 12.6), ("letteralmente", 12.6, 13.2)])
    analysis = Analysis(source="raw.mov", duration=14.0, words=words, silences=[Silence(3.4, 12.0)])

    plan = plan_cut(analysis)

    covered = sum(segment.duration for segment in plan.segments)
    assert covered < 5.0, f"il silenzio è finito nel montaggio: {plan.segments}"


# ------------------------------------------------------------ the restarts


def test_a_phrase_said_twice_in_a_row_is_a_restart():
    script = "E quindi io scommetti su una parte o scommetti su una parte".split()
    words = words_from([(w, i * 0.4, i * 0.4 + 0.3) for i, w in enumerate(script)])
    utterances = takes.build_utterances(words, gap=0.55)

    found = takes.find_restarts(utterances, words)

    assert len(found) == 1
    assert found[0].certain is True
    # Dropped: the abandoned attempt and the stumble between the two.
    kept = " ".join(script[: found[0].first_word] + script[found[0].last_word + 1 :])
    assert kept == "E quindi io scommetti su una parte"


def test_an_utterance_that_opens_the_next_one_is_an_abandoned_start():
    words = words_from(
        [(w, i * 0.4, i * 0.4 + 0.3) for i, w in enumerate("Oppure cerchi di cambiare".split())]
        + [(w, 3.0 + i * 0.4, 3.0 + i * 0.4 + 0.3)
           for i, w in enumerate("Oppure cerchi di cambiare oppure scommetti sull online".split())]
    )
    utterances = takes.build_utterances(words, gap=0.55)
    assert len(utterances) == 2

    found = takes.find_abandoned_starts(utterances)

    assert len(found) == 1
    assert found[0].kind == "prefisso-abbandonato"
    assert found[0].first_word == utterances[0].first_word
    assert found[0].last_word == utterances[0].last_word


def test_a_line_repeated_much_later_is_not_a_restart():
    """A phrase that comes back two minutes on is a refrain, and deleting it
    would remove something the speaker meant to say twice."""
    words = words_from(
        [(w, i * 0.4, i * 0.4 + 0.3) for i, w in enumerate("non ti muovi".split())]
        + [(w, 120.0 + i * 0.4, 120.0 + i * 0.4 + 0.3) for i, w in enumerate("non ti muovi".split())]
    )
    utterances = takes.build_utterances(words, gap=0.55)
    assert takes.find_abandoned_starts(utterances) == []


def test_an_uncertain_restart_is_asked_about_and_changes_nothing_alone():
    """Two words repeated is as likely to be emphasis as a stumble, so it is a
    question — and until it is answered the material stays in."""
    script = "sei bloccata sei bloccata".split()
    words = words_from([(w, i * 0.4, i * 0.4 + 0.3) for i, w in enumerate(script)])
    analysis = Analysis(source="raw.mov", duration=3.0, words=words)

    plan = plan_cut(analysis)

    asked = [q for q in plan.questions if q.id.startswith("restart:")]
    assert len(asked) == 1
    assert asked[0].resolved is False
    assert plan.restarts == []


def test_answering_a_restart_question_removes_it():
    script = "sei bloccata sei bloccata".split()
    words = words_from([(w, i * 0.4, i * 0.4 + 0.3) for i, w in enumerate(script)])
    analysis = Analysis(source="raw.mov", duration=3.0, words=words)
    question = next(q for q in plan_cut(analysis).questions if q.id.startswith("restart:"))

    decided = plan_cut(analysis, None, {question.id: "cut"})

    assert decided.restarts, "la ripartenza non è stata tolta"
    # And the decision stays on the record, like every other one.
    assert next(q for q in decided.questions if q.id == question.id).answer == "cut"


# ------------------------------------------------------------- the harness


def test_the_score_counts_seconds_not_segments():
    score = evaluation.compare([(0.0, 10.0)], [(0.0, 5.0)])
    assert score.recall == 1.0
    assert score.precision == 0.5


def test_overlapping_spans_are_not_counted_twice():
    score = evaluation.compare([(0.0, 6.0), (4.0, 10.0)], [(0.0, 10.0)])
    assert score.proposal_duration == 10.0
    assert score.recall == 1.0


def test_a_boundary_that_moves_slightly_is_not_reported_as_a_difference():
    score = evaluation.compare([(0.0, 5.1)], [(0.0, 5.0)])
    assert score.extra == []


def test_what_is_missing_and_what_is_extra_are_told_apart():
    score = evaluation.compare([(0.0, 4.0)], [(2.0, 8.0)])
    assert score.extra == [(0.0, 2.0)]
    assert score.missing == [(4.0, 8.0)]


# ------------------------------------- the adviser and the engine must agree


class RecordingAdviser:
    """Remembers what it was shown, and points at the first two utterances."""

    name = "prova"

    def __init__(self):
        self.seen: list = []

    def suspects(self, utterances):
        self.seen = list(utterances)
        if len(utterances) < 2:
            return []
        return [{"a": 0, "b": 1, "why": "prova"}]


def _stumbling_take() -> Analysis:
    """Words whose reported ends run into the pauses, as Whisper reports them."""
    script = [
        ("Oppure", 1.0, 1.4), ("cerchi", 1.4, 1.8), ("di", 1.8, 2.0), ("cambiare", 2.0, 6.5),
        ("Oppure", 7.0, 7.4), ("cerchi", 7.4, 7.8), ("di", 7.8, 8.0), ("cambiare", 8.0, 8.5),
        ("davvero", 8.5, 9.0),
    ]
    return Analysis(
        source="raw.mov",
        duration=10.0,
        words=[Word(t=a, end=b, s=text, p=0.95) for text, a, b in script],
        silences=[Silence(2.6, 7.0)],
    )


def test_the_adviser_is_shown_the_utterances_the_engine_will_use():
    """It answers with indices into the list it was given. When the two lists
    were built from different timings — 31 utterances against 50 — every index
    pointed at the wrong line, and the questions read plausibly while being
    about the wrong pair of takes."""
    from skyground.analysis import pipeline

    analysis = _stumbling_take()
    adviser = RecordingAdviser()

    plan = pipeline.propose(analysis, adviser=adviser)

    assert adviser.seen, "il consulente non è stato interpellato"
    assert len(adviser.seen) == len(plan.utterances)
    assert [u.text for u in adviser.seen] == [u.text for u in plan.utterances]


def test_a_suspect_names_the_utterances_the_adviser_meant():
    from skyground.analysis import pipeline

    analysis = _stumbling_take()
    adviser = RecordingAdviser()

    plan = pipeline.propose(analysis, adviser=adviser)

    asked = [q for q in plan.questions if q.id.startswith("take:")]
    assert asked, "il sospetto non è diventato una domanda"
    quoted = " ".join(option.label for option in asked[0].options)
    for shown in (adviser.seen[0].text, adviser.seen[1].text):
        assert shown[:20] in quoted, f"la domanda non parla di «{shown[:30]}»"


def test_preparing_twice_changes_nothing():
    """The engine prepares defensively; a caller that already did must not be
    punished for it."""
    from skyground.analysis import align

    once = align.prepare(_stumbling_take())
    twice = align.prepare(once)
    assert once.words == twice.words
