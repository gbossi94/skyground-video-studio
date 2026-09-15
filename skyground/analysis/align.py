"""Reconciling what the transcriber says with what the audio does.

Whisper reports a start and an end for every word, and those ends are not to be
trusted: on the reference footage it routinely stretches a word across the pause
that follows it. One span of 8.7 seconds contained the two words "stai
letteralmente" and was reported as 97% speech.

The consequence is not cosmetic. The engine decides where to cut by looking for
gaps *between* words, so a pause swallowed by an inflated word is a pause the
engine cannot see — and 123 seconds of dead air and hesitation survived into a
proposal because of it.

The audio itself knows better. `silencedetect` measures loudness and owes
nothing to the language model, so where the two disagree about whether somebody
is speaking, the audio wins.
"""

from __future__ import annotations

from skyground.analysis.models import Silence, Word

#: A word shorter than this is never trimmed further: below it we are arguing
#: with the transcriber about a consonant, not about a pause.
MIN_WORD = 0.06


def _overlapping(silences: list[Silence], start: float, end: float) -> list[Silence]:
    return [s for s in silences if s.end > start and s.start < end]


def clamp_words(words: list[Word], silences: list[Silence]) -> list[Word]:
    """Trim every word back to the speech the audio actually contains.

    A word that runs into a silence is cut at the silence. A word that lies
    entirely inside one is left alone — the transcriber heard *something* there,
    and deleting it would be the engine overruling the words that were said.
    """
    if not silences or not words:
        return list(words)

    ordered = sorted(silences, key=lambda s: s.start)
    corrected: list[Word] = []
    for word in words:
        start, end = word.t, word.end
        for silence in _overlapping(ordered, start, end):
            if silence.start <= start and silence.end >= end:
                break  # wholly inside a silence: leave the word as heard
            if silence.start > start:
                end = min(end, silence.start)
            elif silence.end < end:
                start = max(start, silence.end)
        if end - start < MIN_WORD:
            end = start + MIN_WORD
        corrected.append(Word(t=round(start, 3), end=round(end, 3), s=word.s, p=word.p))
    return corrected


def speech_ratio(words: list[Word], duration: float) -> float:
    """How much of the source the words claim to cover. A sanity check: real
    speech to camera lands around 60%, and a number near 95% means the timings
    have swallowed the pauses."""
    if duration <= 0:
        return 0.0
    return sum(word.end - word.t for word in words) / duration
