"""The cut as a person left it on the timeline.

The engine proposes; a person corrects. A correction is not a question answered
— the questions are the engine's own vocabulary, and re-realising the plan
through them re-splits every long pause and would undo the very boundary the
person just placed. So a hand edit is its own layer: the ordered list of kept
word ranges, each with the seconds a boundary was dragged to, and the plan is
rebuilt from that list by a pure function that passes through the same
invariants as the engine's plans.

Standard library only, no I/O, like the rest of the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from skyground.analysis import align, invariants, takes
from skyground.analysis.cut import (
    CutPolicy,
    _is_filler,
    _label_for,
    _merge_touching,
)
from skyground.analysis.models import (
    REASON_FILLER,
    REASON_LEAD_IN,
    REASON_LEAD_OUT,
    REASON_MANUAL,
    REASON_RETAKE,
    REASON_SILENCE,
    Analysis,
    CutPlan,
    Question,
    Removed,
    Segment,
    Utterance,
)
from skyground.errors import ValidationError

#: Who decided, when it was a person on the timeline rather than the engine
#: (`cut.ENGINE`) or somebody answering a question by email.
MANUAL = "a mano"

#: Timings are milliseconds; compare with the same tolerance as the invariants.
EPSILON = invariants.EPSILON


@dataclass(frozen=True)
class Kept:
    """One run of words that reaches the edit, with where its edges were put.

    `first`/`last` are indices into the *prepared* words (`align.prepare`).
    `start`/`end` are seconds and optional: absent, the policy's lead-in and
    lead-out decide, exactly as the engine would.
    """

    first: int
    last: int
    start: float | None = None
    end: float | None = None

    def as_dict(self) -> dict:
        payload: dict[str, Any] = {"first": self.first, "last": self.last}
        if self.start is not None:
            payload["start"] = round(self.start, 3)
        if self.end is not None:
            payload["end"] = round(self.end, 3)
        return payload

    @staticmethod
    def from_dict(value: dict) -> Kept:
        try:
            first, last = int(value["first"]), int(value["last"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError(
                "ogni pezzo tenuto indica first e last, indici di parola"
            ) from error
        start = value.get("start")
        end = value.get("end")
        return Kept(
            first,
            last,
            None if start is None else float(start),
            None if end is None else float(end),
        )


# ---------------------------------------------------------------- realising


def realise(
    analysis: Analysis,
    previous: CutPlan,
    kept: list[Kept],
    *,
    policy: CutPolicy,
    edited_by: str | None = None,
    now: str | None = None,
    timeline_etag: str | None = None,
) -> CutPlan:
    """The plan a person's kept ranges describe.

    Boundaries are clamped into the gap around their words, so whatever the
    client sent, no cut lands in speech. Anything the person did that the rules
    still forbid — a piece shorter than the policy's minimum — is refused with
    the reason, never silently altered: the timeline must show what was done.
    """
    analysis = align.prepare(analysis)
    words = analysis.words
    _check_ranges(kept, len(words))

    utterances = previous.utterances or takes.build_utterances(words, gap=policy.utterance_gap)
    segments = _segments(words, kept, analysis.duration, policy, utterances)
    _check_lengths(segments, policy.min_segment)

    engine_kept = _engine_kept(previous, words)
    kept_flags = _flags(kept, len(words))
    plan = CutPlan(
        source=analysis.source,
        source_duration=analysis.duration,
        segments=segments,
        policy=dict(previous.policy) or policy.as_dict(),
        generated_at=previous.generated_at or (now or _now()),
    )
    plan.utterances = _with_kept_flags(utterances, kept_flags)
    plan.removed = _removed(
        analysis, segments, words, kept_flags, engine_kept, previous, utterances
    )
    plan.questions = _questions(previous, kept_flags)
    plan.takes = list(previous.takes)
    plan.restarts = list(previous.restarts)
    plan.passages = list(previous.passages)
    plan.editor = dict(previous.editor)
    plan.manual = {
        "kept": [item.as_dict() for item in kept],
        "engineKept": [[first, last] for first, last in engine_kept],
        "editedAt": now or _now(),
        "editedBy": edited_by or "",
        "basedOnTimelineEtag": timeline_etag
        or (previous.manual or {}).get("basedOnTimelineEtag", ""),
    }

    problems = invariants.check(plan, analysis, min_segment=policy.min_segment)
    if problems:
        raise ValidationError(
            "il montaggio a mano non rispetta le regole:\n- "
            + "\n- ".join(str(problem) for problem in problems)
        )
    return plan


def from_plan(plan: CutPlan, words: list) -> list[Kept]:
    """The engine's segments as a person would have laid them: the starting
    point of every hand edit, and the identity `realise(from_plan(p)) == p`."""
    kept = []
    for segment in plan.segments:
        first, last = segment.first_word, segment.last_word
        if first is None or last is None:
            inside = _words_inside(words, segment.start, segment.end)
            if not inside:
                continue
            first, last = inside[0], inside[-1]
        kept.append(Kept(first, last, segment.start, segment.end))
    return kept


def from_timeline(analysis: Analysis, clips: list[dict]) -> tuple[list[Kept], list[str]]:
    """A timeline written by hand, or restored from an old revision, as kept
    ranges — with a note for every clip that had to be adjusted to fit."""
    analysis = align.prepare(analysis)
    words = analysis.words
    kept: list[Kept] = []
    notes: list[str] = []
    last_word = -1
    for index, clip in enumerate(clips):
        start, end = float(clip["start"]), float(clip["end"])
        inside = [i for i in _words_inside(words, start, end) if i > last_word]
        if not inside:
            notes.append(f"clip {index + 1} ({start:.2f}–{end:.2f}s) senza parole intere: saltata")
            continue
        for position, side in ((start, "inizio"), (end, "fine")):
            for word in words:
                if word.t + EPSILON < position < word.end - EPSILON:
                    notes.append(
                        f"clip {index + 1}: {side} dentro «{word.s}», "
                        "spostato al bordo della parola"
                    )
        kept.append(Kept(inside[0], inside[-1], start, end))
        last_word = inside[-1]
    return kept, notes


def with_answer(kept: list[Kept], first: int, last: int, keep: bool) -> list[Kept]:
    """A question's answer laid over the kept ranges: `cut` drops the words
    `first..last` from wherever they are, `keep` puts them back as a range."""
    if keep:
        merged = list(kept) + [Kept(first, last)]
        merged.sort(key=lambda item: item.first)
        joined: list[Kept] = []
        for item in merged:
            if joined and item.first <= joined[-1].last + 1:
                tail = joined.pop()
                item = Kept(
                    tail.first,
                    max(tail.last, item.last),
                    tail.start,
                    tail.end if tail.last >= item.last else item.end,
                )
            joined.append(item)
        return joined
    result: list[Kept] = []
    for item in kept:
        if item.last < first or item.first > last:
            result.append(item)
            continue
        if item.first < first:
            result.append(Kept(item.first, first - 1, item.start, None))
        if item.last > last:
            result.append(Kept(last + 1, item.last, None, item.end))
    return result


# ------------------------------------------------------------------ pieces


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _check_ranges(kept: list[Kept], total: int) -> None:
    previous = -1
    for index, item in enumerate(kept):
        if item.first < 0 or item.last >= total or item.first > item.last:
            raise ValidationError(
                f"pezzo {index + 1}: parole {item.first}–{item.last} fuori dalla trascrizione "
                f"({total} parole)"
            )
        if item.first <= previous:
            raise ValidationError(
                f"pezzo {index + 1} comincia alla parola {item.first}, prima della fine del "
                f"pezzo precedente ({previous}): i pezzi restano nell'ordine del girato"
            )
        previous = item.last


def _segments(
    words, kept: list[Kept], duration: float, policy: CutPolicy, utterances
) -> list[Segment]:
    segments: list[Segment] = []
    for item in kept:
        previous_end = segments[-1].end if segments else 0.0
        lo = max(words[item.first - 1].end if item.first > 0 else 0.0, previous_end, 0.0)
        hi = words[item.first].t
        wanted = words[item.first].t - policy.lead_in if item.start is None else item.start
        start = min(max(wanted, lo), hi)

        lo = words[item.last].end
        hi = min(words[item.last + 1].t if item.last + 1 < len(words) else duration, duration)
        wanted = words[item.last].end + policy.lead_out if item.end is None else item.end
        end = min(max(wanted, lo), hi)

        segments.append(
            Segment(
                start=start,
                end=end,
                label=_label_for(utterances, item.first),
                first_word=item.first,
                last_word=item.last,
            )
        )
    return _merge_touching(segments)


def _check_lengths(segments: list[Segment], min_segment: float) -> None:
    short = [
        f"il pezzo {index + 1} ({segment.start:.2f}–{segment.end:.2f}s) dura "
        f"{segment.duration:.2f}s, minimo {min_segment:.2f}s"
        for index, segment in enumerate(segments)
        if segment.duration < min_segment - EPSILON
    ]
    if short:
        raise ValidationError("; ".join(short))


def _flags(kept: list[Kept], total: int) -> list[bool]:
    flags = [False] * total
    for item in kept:
        for index in range(item.first, item.last + 1):
            flags[index] = True
    return flags


def _engine_kept(previous: CutPlan, words) -> list[tuple[int, int]]:
    """Which words the engine kept, remembered once: after that the person's
    layer is the only thing that changes, and the engine's removals keep
    their reasons no matter how the edit moves around them."""
    stored = (previous.manual or {}).get("engineKept")
    if stored:
        return [(int(first), int(last)) for first, last in stored]
    return [(item.first, item.last) for item in from_plan(previous, words)]


def _with_kept_flags(utterances: list[Utterance], kept: list[bool]) -> list[Utterance]:
    result = []
    for utterance in utterances:
        flags = kept[utterance.first_word : utterance.last_word + 1]
        is_kept = any(flags)
        result.append(
            Utterance(
                index=utterance.index,
                first_word=utterance.first_word,
                last_word=utterance.last_word,
                start=utterance.start,
                end=utterance.end,
                text=utterance.text,
                take_group=utterance.take_group,
                kept=is_kept,
                drop_reason="" if is_kept else (utterance.drop_reason or "tolto a mano"),
            )
        )
    return result


def _words_inside(words, start: float, end: float) -> list[int]:
    return [
        index
        for index, word in enumerate(words)
        if word.t >= start - EPSILON and word.end <= end + EPSILON
    ]


def _removed(analysis, segments, words, kept, engine_kept, previous, utterances) -> list[Removed]:
    """Everything outside the segments, each gap with its reason: the engine's
    where the engine removed it, the person's where the person did."""
    engine_flags = [False] * len(words)
    for first, last in engine_kept:
        for index in range(first, last + 1):
            engine_flags[index] = True

    removed: list[Removed] = []
    cursor = 0.0
    edges = [(segment.start, segment.end) for segment in segments] + [
        (analysis.duration, analysis.duration)
    ]
    for start, end in edges:
        if start > cursor + 1e-6:
            removed.append(
                _describe(analysis, cursor, start, words, engine_flags, previous, utterances)
            )
        cursor = max(cursor, end)
    return [item for item in removed if item.duration > 1e-6]


def _describe(analysis, start, end, words, engine_flags, previous, utterances) -> Removed:
    inside = _words_inside(words, start, end)
    if not inside:
        if start <= 1e-6:
            return Removed(start, end, REASON_LEAD_IN, 1.0, "silenzio prima della prima battuta")
        if end >= analysis.duration - 1e-6:
            return Removed(start, end, REASON_LEAD_OUT, 1.0, "silenzio dopo l'ultima battuta")
        return Removed(start, end, REASON_SILENCE, 1.0, f"pausa di {end - start:.2f}s")

    said = " ".join(words[index].s for index in inside)
    if any(engine_flags[index] for index in inside):
        # Words the engine wanted in the film and the person took out.
        return Removed(start, end, REASON_MANUAL, 1.0, f"tolto a mano: «{said[:80]}»")

    # The engine removed these words: its reason travels with them.
    for item in previous.removed:
        if item.reason in (REASON_SILENCE, REASON_LEAD_IN, REASON_LEAD_OUT):
            continue
        if item.start < end - 1e-6 and item.end > start + 1e-6:
            return Removed(start, end, item.reason, item.confidence, item.detail)
    spoken = [u for u in utterances if u.first_word <= inside[-1] and u.last_word >= inside[0]]
    reason = REASON_FILLER if spoken and all(_is_filler(u.text) for u in spoken) else REASON_RETAKE
    return Removed(start, end, reason, 0.9, f"scartata dal motore: «{said[:80]}»")


def _questions(previous: CutPlan, kept: list[bool]) -> list[Question]:
    """The engine's questions, with the `edit:` ones re-read from the timeline:
    a piece the person put back is answered `keep`, and says who."""
    questions: list[Question] = []
    for original in previous.questions:
        question = Question.from_dict(original.as_dict())
        if question.id.startswith("edit:"):
            try:
                first, last = (int(part) for part in question.id[5:].split("-"))
            except ValueError:
                questions.append(question)
                continue
            inside = kept[first : last + 1]
            answer = "keep" if any(inside) else "cut"
            if answer != question.answer:
                question.answer = answer
                question.answered_by = MANUAL
        questions.append(question)
    questions.sort(key=lambda item: item.at)
    return questions
