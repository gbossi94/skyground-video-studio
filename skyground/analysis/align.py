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

#: Quanto di una parola si lascia *dopo* l'inizio del silenzio misurato. Le
#: code sonore — il «-go» di «fango», il «-te» di «frustrante» — scendono sotto
#: la soglia del rilevatore prima di finire davvero: tagliare esattamente dove
#: il rilevatore tace mozza la parola, e una persona lo ha sentito. Il
#: rilevatore serve a trovare le parole *stirate* sopra una pausa (una da
#: undici secondi), non a rifilare le finali; due decimi di silenzio in più
#: non costano niente e la parola arriva intera.
TAIL = 0.20

#: How much of a silence is left *before* the detector says speech resumes. A
#: quiet onset — the «S» of «Se», the «f» of «faremo» — sits under the
#: detector's threshold: the silence it reports ends when the vowel arrives,
#: and a word snapped to that end lost its first consonant. A tenth and a half
#: of silence is silence, and the consonant arrives whole.
ONSET = 0.15

#: A silence broken for less than this is one silence. A lip smack between two
#: reported silences is not speech, and a word start snapped to it — as one was,
#: on the TEST footage — put the smack at the head of the clip.
BLIP = 0.05

#: A word whose speech before a pause is shorter than this (and a small share
#: of the word) was anchored to the previous word by the transcriber: «se»
#: reported at the very start of a 3.6-second pause, its real self on the far
#: side. Such a word belongs after the pause, not trimmed to a stub before it.
STUB = 0.08

#: A word reported wholly inside a silence but ending within this of the
#: silence's end is the quiet onset of what follows it, and is moved there. A
#: word deep inside a long silence is left where the transcriber put it.
NEAR_END = 0.10


def _overlapping(silences: list[Silence], start: float, end: float) -> list[Silence]:
    return [s for s in silences if s.end > start and s.start < end]


def merge_blips(silences: list[Silence]) -> list[Silence]:
    """Silences separated by less than `BLIP` are one silence."""
    merged: list[Silence] = []
    for silence in sorted(silences, key=lambda s: s.start):
        if merged and silence.start - merged[-1].end < BLIP:
            merged[-1] = Silence(merged[-1].start, max(merged[-1].end, silence.end))
        else:
            merged.append(silence)
    return merged


def clamp_words(words: list[Word], silences: list[Silence]) -> list[Word]:
    """Trim every word back to the speech the audio actually contains.

    The transcriber's timings drift in three ways the audio can tell apart. A
    word stretched over the pause that follows it is cut at the silence (with a
    tail, see `TAIL`). A word that *starts* early, swallowing the pause before
    it — «stellare» reported from the last vowel of «veramente» — has a silence
    inside it with more speech after than before: it moves to the far side. A
    word anchored to the previous one with almost nothing before the pause is a
    stub: it moves too. A word reported wholly inside a silence is kept — the
    transcriber heard *something* — and, when it ends where the silence ends,
    hugged to that end, because that is the quiet onset of a word.
    """
    if not silences or not words:
        return list(words)

    ordered = merge_blips(silences)
    corrected: list[Word] = []
    for word in words:
        start, end = word.t, word.end
        for silence in _overlapping(ordered, start, end):
            if silence.start <= start and silence.end >= end:
                # Wholly inside: never deleted. Near the end, it is the onset
                # the detector could not hear; deep inside, it stays as heard.
                if silence.end - end <= NEAR_END:
                    start = max(start, silence.end - ONSET)
                    end = max(end, start + MIN_WORD)
                break
            if silence.start > start:
                before = silence.start - start
                after = max(0.0, end - silence.end)
                if after > before and after >= STUB:
                    # The pause is inside the reported span and the word is on
                    # the far side of it.
                    start = max(start, silence.end - ONSET)
                elif before < STUB and before < 0.25 * (end - start):
                    # A stub before the pause: the word is on the far side.
                    start = silence.end - ONSET
                    end = max(end, start + MIN_WORD)
                else:
                    end = min(end, silence.start + TAIL)
            elif silence.end < end:
                start = max(start, silence.end - ONSET)
        if end - start < MIN_WORD:
            end = start + MIN_WORD
        corrected.append(Word(t=round(start, 3), end=round(end, 3), s=word.s, p=word.p))
    return _in_order(corrected)


def _in_order(words: list[Word]) -> list[Word]:
    """No word may run into the one after it.

    Trimming can push a word's end past the start of the next: a word squeezed
    almost to nothing by a silence is widened back to `MIN_WORD`, and that
    minimum has to come from somewhere. Thirty-eight of them overlapped on the
    reference take.

    It matters because the rule that no cut lands inside a word is enforced by
    padding a segment only as far as the neighbouring word — which assumes the
    neighbour is where it claims to be. With overlaps the plan was built,
    checked, and refused by its own invariants at the last moment.
    """
    ordered: list[Word] = []
    for word in words:
        start, end = word.t, word.end
        if ordered and start < ordered[-1].end:
            start = ordered[-1].end
            end = max(end, start + MIN_WORD)
        ordered.append(Word(t=round(start, 3), end=round(end, 3), s=word.s, p=word.p))
    return ordered


def prepare(analysis):
    """The analysis everything downstream must reason about.

    There has to be exactly one corrected version, produced in one place. When
    there were two — the adviser reading the raw timings and the engine reading
    the corrected ones — they disagreed about how many utterances the take
    contains, and every index the adviser returned pointed at the wrong line.
    The questions that came out read plausibly and were nonsense.

    Idempotent: a word already trimmed to the speech around it has nothing left
    to trim, so calling this twice is safe and calling it once is enough.
    """
    from dataclasses import replace

    return replace(analysis, words=clamp_words(analysis.words, analysis.silences))


def speech_ratio(words: list[Word], duration: float) -> float:
    """How much of the source the words claim to cover. A sanity check: real
    speech to camera lands around 60%, and a number near 95% means the timings
    have swallowed the pauses."""
    if duration <= 0:
        return 0.0
    return sum(word.end - word.t for word in words) / duration
