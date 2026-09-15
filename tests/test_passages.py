"""Whole passages said twice, and choosing between them.

The measurement that forced this module into existence: with the engine
deciding on its own and comparing utterances one at a time, it proposed 135.8s
against the 115.9s a person approved. Twenty-three of those excess seconds were
one thing — a stretch of script delivered, abandoned, and delivered again. The
engine found the one matching *line* in the middle of it, dropped that line, and
left the rest of the discarded attempt in the edit.

A passage is the unit an editor actually chooses between. These tests pin the
three parts of getting that right: finding the pair, bounding the second
attempt, and picking the better one without ever asking which came first.
"""

from __future__ import annotations

from skyground.analysis import takes
from skyground.analysis.cut import CutPolicy, plan_cut
from skyground.analysis.models import Analysis, Word


def speak(script: list[tuple[str, float]], *, word: float = 0.3) -> list[Word]:
    """Words at the times given, each lasting `word` seconds."""
    return [Word(t=at, end=at + word, s=text, p=0.95) for text, at in script]


def read(line: str, start: float, *, pace: float = 0.4) -> list[tuple[str, float]]:
    return [(token, start + index * pace) for index, token in enumerate(line.split())]


def utterances_of(script: list[tuple[str, float]]):
    return takes.build_utterances(speak(script), gap=0.55)


# ------------------------------------------------------------------- trovarli


def test_a_stretch_said_twice_is_one_pair_not_three():
    """Three sentences, then the same three again. The old code found the one
    middle pair that happened to match; this finds the passage."""
    first = read("o scommetti su una parte di clienti buoni", 0.0)
    first += read("con cui vorresti lavorare tutti i giorni", 4.0)
    second = read("o scommetti su una parte di clienti che vale", 10.0)
    second += read("ti piacciono ma non sai dove trovarli", 15.0)

    found = takes.find_retaken_passages(utterances_of(first + second))

    assert len(found) == 1
    assert found[0].first.utterances == [0, 1]
    assert found[0].second.utterances == [2, 3]


def test_the_second_attempt_runs_to_the_pause_not_to_the_last_match():
    """It used to stop at the first line with nothing to match against — which
    is precisely the line the speaker got right on the second go. Cut short,
    the retake looked unfinished, and the engine kept the abandoned one."""
    first = read("e poi in ogni caso non devi", 0.0)
    second = read("e poi in ogni caso non è proprio un salto nel vuoto", 5.0)
    second += read("fissano con noi per venti minuti e capisci tutto", 11.0)

    found = takes.find_retaken_passages(utterances_of(first + second))

    assert len(found) == 1
    # The whole second delivery, including the half it managed to finish.
    assert len(found[0].second.utterances) == 2


def test_a_long_silence_ends_the_retake():
    """Without this the passage runs on into whatever came next and takes it
    with it — the one way this can delete something nobody said twice."""
    first = read("oppure cerchi di cambiare del tutto", 0.0)
    first += read("perché il mestiere non ti piace più", 4.0)
    second = read("oppure cerchi di cambiare davvero mestiere", 8.0)
    elsewhere = read("adesso parliamo di una cosa completamente diversa", 30.0)

    found = takes.find_retaken_passages(utterances_of(first + second + elsewhere))

    assert len(found) == 1
    assert found[0].second.end < 20.0


def test_three_attempts_chain_instead_of_pairing_off():
    """Two at a time leaves the middle one standing in the edit."""
    script = read("e poi in ogni caso non devi", 0.0)
    script += read("e poi in ogni caso non è proprio un salto", 5.0)
    script += read("e in ogni caso non è proprio una scommessa nel vuoto", 12.0)

    found = takes.find_retaken_passages(utterances_of(script))

    assert len(found) == 2
    assert found[0].second.utterances == found[1].first.utterances


def test_what_comes_next_is_not_a_retake_of_what_came_before():
    """The guard that matters most: a wrong pairing here deletes a sentence
    somebody meant to say. Shared vocabulary is not shared opening."""
    script = read("prima parte della frase detta bene", 0.0)
    script += read("seconda parte della frase detta bene", 4.0)

    assert takes.find_retaken_passages(utterances_of(script)) == []


# -------------------------------------------------------------------- sceglere


def test_the_cleaner_delivery_wins_even_when_it_came_first():
    """«Keep the last» is the rule this project knows to be wrong. The second
    attempt here disintegrates into fragments; the first does not."""
    first = read("o scommetti su una parte di clienti buoni con cui vorresti", 0.0)
    first += read("lavorare tutti i giorni ma non ne sono così tanti", 5.0)
    second = read("o scommetti su una parte di clienti che vale di più", 12.0)
    second += [("base", 18.0)]
    second += [("o", 20.0)]

    utterances = utterances_of(first + second)
    retake = takes.find_retaken_passages(utterances)[0]
    left, right = takes.score_passages(retake, utterances, speak(first + second))

    assert left > right


def test_a_flawless_four_words_loses_to_the_attempt_that_said_more():
    """«E quindi cosa fai?» is a complete, fluent sentence — and it is the one
    to drop. Without reach it scored a perfect 1.0 and won every time."""
    first = read("e quindi cosa fai", 0.0)
    second = read("e quindi che cosa fai devi fare una scommessa", 3.0)

    utterances = utterances_of(first + second)
    retake = takes.find_retaken_passages(utterances)[0]
    left, right = takes.score_passages(retake, utterances, speak(first + second))

    assert right > left


# ----------------------------------------------------------------- nel motore


def _two_takes() -> Analysis:
    first = read("o scommetti su una parte di clienti buoni con cui vorresti", 0.0)
    first += read("lavorare tutti i giorni ma non ne sono così tanti", 5.0)
    second = read("o scommetti su una parte di clienti che vale di più", 12.0)
    second += [("base", 18.0)]
    return Analysis(source="raw.mov", duration=25.0, words=speak(first + second))


def test_the_engine_drops_the_whole_losing_passage():
    plan = plan_cut(_two_takes())

    dropped = [item for item in plan.utterances if not item.kept]
    assert [item.index for item in dropped] == [2, 3]
    assert all("passaggio" in item.drop_reason for item in dropped)
    assert plan.passages, "la decisione non è finita nel piano"


def test_the_decision_is_on_the_record_and_can_be_changed():
    plan = plan_cut(_two_takes())
    question = next(item for item in plan.questions if item.id.startswith("passage:"))
    assert question.answered_by == "motore"
    assert question.answer == "first"

    other = plan_cut(_two_takes(), None, {question.id: "second"})

    dropped = {item.index for item in other.utterances if not item.kept}
    assert dropped == {0, 1}, "cambiare risposta non ha cambiato il montaggio"


def test_nothing_is_dropped_when_asked_to_wait():
    plan = plan_cut(_two_takes(), CutPolicy(ask_when_unsure=True, decide_margin=0.9))

    question = next(item for item in plan.questions if item.id.startswith("passage:"))
    assert question.resolved is False
    assert all(item.kept for item in plan.utterances)
