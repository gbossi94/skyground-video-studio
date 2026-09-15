"""Finding the repeated attempts and judging which one is the better take.

A person recording to camera says the same line two, three, four times. The
edit keeps one. Which one is not a matter of position: in the reference footage
the editor kept the *first* attempt of the opening line and the *second* attempt
of a later one. Any rule of the form "keep the last" is therefore wrong, and a
rule that is wrong silently is the thing this project cannot have.

So this module does two separate jobs and keeps them separate: it groups the
attempts, which can be done reliably, and it *scores* them, which cannot. The
score produces a recommendation and a margin; the engine turns a small margin
into a question instead of a decision.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from skyground.analysis.models import TakeGroup, Utterance, Word

#: Hesitations and discourse particles. Their presence lowers a take's score;
#: removing one from the middle of a sentence is a separate, asked-about decision.
FILLERS = frozenset(
    {
        "ehm", "eh", "ah", "mmh", "mh", "uhm", "boh",
        "cioè", "tipo", "insomma", "praticamente", "diciamo", "niente",
    }
)

#: A take that trails off without one of these rarely works as a final line.
#:
#: Note what is *not* here: the ellipsis. A transcriber writes «…» exactly where
#: the speaker broke off — «e poi in ogni caso non devi…» — so reading it as a
#: finished sentence inverts the signal. It did: the engine was recommending the
#: abandoned attempt over the one that was actually delivered, and answering as
#: recommended lost a quarter of the approved edit.
SENTENCE_END = re.compile(r"[.!?]$")

#: …and the ellipsis is positive evidence of the opposite.
TRAILS_OFF = re.compile(r"(…|\.\.\.)$")

#: Below this fraction of the longest attempt's words, a take has said too
#: little of the line to count as a finished one.
PARTIAL_TAKE = 0.6

WORD_SPLIT = re.compile(r"[^\w']+", re.UNICODE)


def normalize(text: str) -> str:
    """Fold case and accents so that two attempts compare on their words alone."""
    lowered = text.lower().strip()
    decomposed = unicodedata.normalize("NFD", lowered)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped)


def tokens(text: str) -> list[str]:
    return [token for token in WORD_SPLIT.split(normalize(text)) if token]


# ------------------------------------------------------------------ utterances


def build_utterances(words: list[Word], *, gap: float = 0.55) -> list[Utterance]:
    """Split the transcript wherever the speaker pauses long enough to restart."""
    utterances: list[Utterance] = []
    if not words:
        return utterances
    start_index = 0
    for index in range(1, len(words) + 1):
        finished = index == len(words)
        if not finished and words[index].t - words[index - 1].end <= gap:
            continue
        last_index = index - 1
        chunk = words[start_index : last_index + 1]
        utterances.append(
            Utterance(
                index=len(utterances),
                first_word=start_index,
                last_word=last_index,
                start=chunk[0].t,
                end=chunk[-1].end,
                text=" ".join(word.s for word in chunk),
            )
        )
        start_index = index
    return utterances


# ------------------------------------------------------------------ similarity


def _lcs_length(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for l_token in left:
        current = [0]
        for column, r_token in enumerate(right):
            if l_token == r_token:
                current.append(previous[column] + 1)
            else:
                current.append(max(previous[column + 1], current[column]))
        previous = current
    return previous[-1]


def _prefix_match(left: list[str], right: list[str]) -> int:
    count = 0
    for l_token, r_token in zip(left, right, strict=False):
        if l_token != r_token:
            break
        count += 1
    return count


def similarity(left: str, right: str) -> float:
    """How likely two utterances are two attempts at the same line.

    Three signals, because none of them is enough alone: shared vocabulary
    catches a reworded attempt, the longest common subsequence catches an
    attempt with an insertion, and a shared opening catches the very common case
    of starting the sentence again from the top.
    """
    left_tokens, right_tokens = tokens(left), tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shortest = min(len(left_tokens), len(right_tokens))

    left_set, right_set = set(left_tokens), set(right_tokens)
    jaccard = len(left_set & right_set) / len(left_set | right_set)
    subsequence = _lcs_length(left_tokens, right_tokens) / shortest
    prefix = _prefix_match(left_tokens, right_tokens) / shortest

    return max(0.45 * jaccard + 0.55 * subsequence, prefix)


# -------------------------------------------------------------------- restarts


@dataclass(frozen=True)
class Restart:
    """A run-up the speaker abandoned and said again.

    Not a retake between two utterances — those are handled by the grouping
    below — but the far more common thing a person does when talking to camera:
    they start a clause, stumble, and start it again without pausing long enough
    to make it a separate utterance. The engine used to be blind to this, and on
    the reference footage that blindness was most of the material an editor cut.
    """

    #: The words to drop, as indices into the transcript. Inclusive.
    first_word: int
    last_word: int
    kind: str
    detail: str
    #: True when the repetition is literal enough to act on without asking.
    certain: bool

    @property
    def word_count(self) -> int:
        return self.last_word - self.first_word + 1


#: A literal repeat of at least this many words is a restart, not rhetoric.
CERTAIN_REPEAT = 3
#: How many words may sit between the abandoned attempt and the new one. More
#: than this and the two occurrences are probably both meant.
MAX_STUMBLE = 3


def find_restarts(utterances: list[Utterance], words: list[Word]) -> list[Restart]:
    """Find, inside each utterance, a phrase said twice in a row."""
    found: list[Restart] = []
    for utterance in utterances:
        chunk = words[utterance.first_word : utterance.last_word + 1]
        forms = [normalize(word.s).strip(".,!?…'\"") for word in chunk]
        best = _longest_repeat(forms)
        if best is None:
            continue
        start, size, second = best
        # Everything from the first attempt up to the second one is the run-up.
        first_word = utterance.first_word + start
        last_word = utterance.first_word + second - 1
        skipped = second - (start + size)
        phrase = " ".join(forms[second : second + size])
        found.append(
            Restart(
                first_word=first_word,
                last_word=last_word,
                kind="ripartenza",
                detail=f"«{phrase}» detto due volte di fila",
                certain=size >= CERTAIN_REPEAT and skipped <= 1,
            )
        )
    return found


def _longest_repeat(forms: list[str]) -> tuple[int, int, int] | None:
    """The longest phrase repeated close to itself: (start, length, second start)."""
    limit = min(8, len(forms) // 2)
    for size in range(limit, 1, -1):
        for start in range(len(forms) - size * 2 + 1):
            first = forms[start : start + size]
            if not any(token for token in first):
                continue
            for second in range(start + size, min(start + size + MAX_STUMBLE + 1, len(forms) - size + 1)):
                if forms[second : second + size] == first:
                    return start, size, second
    return None


def find_abandoned_starts(
    utterances: list[Utterance], *, within_seconds: float = 6.0
) -> list[Restart]:
    """An utterance that is the opening of the next one, said and left.

    «Oppure cerchi di cambiare.» followed by «Oppure cerchi di cambiare, oppure
    scommetti sull'online…» is one line, attempted twice. Similarity alone does
    not catch it: the two are very different in length, which is exactly what
    the ratio-based score punishes.
    """
    found: list[Restart] = []
    for current, following in zip(utterances, utterances[1:], strict=False):
        if following.start - current.end > within_seconds:
            continue
        head, tail = tokens(current.text), tokens(following.text)
        if len(head) < 2 or len(tail) <= len(head):
            continue
        if tail[: len(head)] != head:
            continue
        found.append(
            Restart(
                first_word=current.first_word,
                last_word=current.last_word,
                kind="prefisso-abbandonato",
                detail=(
                    f"«{current.text[:60]}» è l'inizio della battuta che segue, "
                    "lasciata a metà"
                ),
                certain=len(head) >= CERTAIN_REPEAT,
            )
        )
    return found


# ---------------------------------------------------------------------- groups


def group_takes(
    utterances: list[Utterance],
    *,
    threshold: float = 0.62,
    lookahead: int = 4,
    window_seconds: float = 120.0,
) -> list[TakeGroup]:
    """Cluster nearby utterances that are attempts at the same line.

    Only nearby ones: a phrase legitimately repeated two minutes later is a
    rhetorical echo, not a retake, and merging the two would delete a line the
    speaker meant to say twice.
    """
    groups: list[TakeGroup] = []
    assigned: dict[int, int] = {}

    for position, utterance in enumerate(utterances):
        for other in utterances[position + 1 : position + 1 + lookahead]:
            if other.start - utterance.end > window_seconds:
                break
            if similarity(utterance.text, other.text) < threshold:
                continue
            group_id = assigned.get(utterance.index)
            if group_id is None:
                group_id = len(groups)
                groups.append(TakeGroup(id=group_id, utterances=[utterance.index]))
                assigned[utterance.index] = group_id
            if other.index not in assigned:
                groups[group_id].utterances.append(other.index)
                assigned[other.index] = group_id

    for group in groups:
        group.utterances.sort()
        for index in group.utterances:
            utterances[index].take_group = group.id
    return groups


# --------------------------------------------------------------------- scoring


@dataclass(frozen=True)
class TakeScore:
    total: float
    complete: float
    fluency: float
    confidence: float
    length: float
    recency: float

    def as_dict(self) -> dict:
        return {
            "total": round(self.total, 4),
            "complete": round(self.complete, 4),
            "fluency": round(self.fluency, 4),
            "confidence": round(self.confidence, 4),
            "length": round(self.length, 4),
            "recency": round(self.recency, 4),
        }


def score_take(
    utterance: Utterance, words: list[Word], siblings: list[Utterance], position: int
) -> TakeScore:
    """Judge one attempt. Every term is a stated opinion, not a fact.

    The weights are deliberately mild: the purpose of the score is to order the
    options shown to a person, not to be trusted on its own.
    """
    chunk = words[utterance.first_word : utterance.last_word + 1]
    count = max(1, len(chunk))

    said = utterance.text.strip()
    if TRAILS_OFF.search(said):
        complete = 0.0  # broke off mid-thought: whatever follows is the real take
    elif SENTENCE_END.search(said):
        complete = 1.0
    else:
        complete = 0.35

    filler_count = sum(1 for word in chunk if normalize(word.s).strip(".,!?'") in FILLERS)
    fluency = max(0.0, 1.0 - (filler_count / count) * 4.0)

    confidence = sum(word.p for word in chunk) / count

    # A take much shorter than its siblings said less of the line, which is a
    # fact about the attempt rather than a preference about it. The penalty used
    # to be gentle — a third of the words still scored 0.58 — and a tidy fragment
    # ending in a full stop could out-score the attempt that actually delivered
    # the thought. «ancora alzando i prezzi.» beat the sentence it was the tail
    # of, and six seconds of the approved edit went with it.
    longest = max((sibling.word_count for sibling in siblings), default=utterance.word_count)
    length = utterance.word_count / max(1, longest)

    # A take that said a fraction of the line cannot claim to have finished it,
    # whatever punctuation it ends on. Without this, «ancora alzando i prezzi.»
    # — four words and a full stop — outscored the sentence it was the tail of.
    if length < PARTIAL_TAKE:
        complete = min(complete, 0.35)

    # Later attempts are somewhat more likely to be the good one — but only
    # somewhat, because the reference footage shows the opposite happening.
    recency = position / max(1, len(siblings) - 1) if len(siblings) > 1 else 1.0

    # How much of the line a take delivered carries real weight now. Position
    # carries almost none: this codebase's own reference footage has the editor
    # keeping the first attempt of one line and the second of another, so
    # "later is better" is a coin toss dressed up as a rule.
    total = (
        0.28 * complete
        + 0.22 * fluency
        + 0.18 * confidence
        + 0.27 * length
        + 0.05 * recency
    )
    return TakeScore(total, complete, fluency, confidence, length, recency)


def rank_group(
    group: TakeGroup, utterances: list[Utterance], words: list[Word]
) -> tuple[int, float, dict[int, TakeScore]]:
    """Return the recommended utterance, the margin over the runner-up, and the
    detail behind every score so the editor can see the reasoning."""
    siblings = [utterances[index] for index in group.utterances]
    scores = {
        index: score_take(utterances[index], words, siblings, position)
        for position, index in enumerate(group.utterances)
    }
    ordered = sorted(scores.items(), key=lambda item: item[1].total, reverse=True)
    best_index, best_score = ordered[0]
    margin = best_score.total - ordered[1][1].total if len(ordered) > 1 else 1.0
    return best_index, margin, scores
