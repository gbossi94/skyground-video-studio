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
SENTENCE_END = re.compile(r"[.!?…]$")

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

    complete = 1.0 if SENTENCE_END.search(utterance.text.strip()) else 0.35

    filler_count = sum(1 for word in chunk if normalize(word.s).strip(".,!?'") in FILLERS)
    fluency = max(0.0, 1.0 - (filler_count / count) * 4.0)

    confidence = sum(word.p for word in chunk) / count

    # A take much shorter than its siblings is usually an aborted start; one much
    # longer is usually the speaker rambling before finding the line.
    longest = max((sibling.word_count for sibling in siblings), default=utterance.word_count)
    ratio = utterance.word_count / max(1, longest)
    length = 1.0 - abs(1.0 - ratio) * 0.6

    # Later attempts are somewhat more likely to be the good one — but only
    # somewhat, because the reference footage shows the opposite happening.
    recency = position / max(1, len(siblings) - 1) if len(siblings) > 1 else 1.0

    total = (
        0.30 * complete
        + 0.25 * fluency
        + 0.20 * confidence
        + 0.15 * length
        + 0.10 * recency
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
