"""The rules a cut plan may never break.

The engine proposes, an adviser may reorder its preferences, a person answers
the open questions — but every plan, whatever produced it, passes through here
before it can become a timeline. A violation is a bug in the engine, not a
warning for the editor: `apply` refuses the plan outright.

Keeping the rules in one place, as data, is what makes them testable against the
real footage rather than trusted by inspection.
"""

from __future__ import annotations

from dataclasses import dataclass

from skyground.analysis.models import REASONS, Analysis, CutPlan

#: Timings come from a transcript with millisecond resolution; compare with the
#: same granularity instead of demanding exact float equality.
EPSILON = 0.0015


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str
    at: float = 0.0

    def __str__(self) -> str:
        return f"[{self.rule}] {self.detail} (a {self.at:.3f}s)"


def check(plan: CutPlan, analysis: Analysis, *, min_segment: float = 0.3) -> list[Violation]:
    """Return every rule the plan breaks. An empty list means it is applicable."""
    problems: list[Violation] = []
    problems += _ordering(plan)
    problems += _within_source(plan, analysis)
    problems += _minimum_length(plan, min_segment)
    problems += _never_inside_a_word(plan, analysis)
    problems += _words_are_whole(plan, analysis)
    problems += _coverage(plan, analysis)
    problems += _reasons(plan)
    return problems


def applicable(plan: CutPlan, analysis: Analysis, **kwargs) -> tuple[bool, list[str]]:
    """Whether the plan can be turned into a timeline right now.

    Two distinct ways of not being applicable: it is broken (a violation), or it
    is undecided (an open question). The second is not an error — it is the
    engine refusing to guess — so it is reported separately.
    """
    problems = [str(item) for item in check(plan, analysis, **kwargs)]
    if not plan.manual:
        # A cut a person laid out by hand answers every question at once: what
        # is on the timeline is the decision.
        for question in plan.open_questions:
            problems.append(f"[domanda aperta] {question.prompt}")
    return (not problems), problems


# ----------------------------------------------------------------- the rules


def _ordering(plan: CutPlan) -> list[Violation]:
    """Segments run forward and never overlap."""
    problems = []
    previous_end = None
    for index, segment in enumerate(plan.segments):
        if segment.end <= segment.start:
            problems.append(
                Violation("segmento-vuoto", f"il segmento {index} non ha durata", segment.start)
            )
        if previous_end is not None and segment.start < previous_end - EPSILON:
            problems.append(
                Violation(
                    "segmenti-sovrapposti",
                    f"il segmento {index} inizia prima della fine del precedente",
                    segment.start,
                )
            )
        previous_end = segment.end
    return problems


def _within_source(plan: CutPlan, analysis: Analysis) -> list[Violation]:
    problems = []
    for index, segment in enumerate(plan.segments):
        if segment.start < -EPSILON:
            problems.append(
                Violation(
                    "fuori-sorgente", f"il segmento {index} inizia prima di 0", segment.start
                )
            )
        if segment.end > analysis.duration + EPSILON:
            problems.append(
                Violation(
                    "fuori-sorgente", f"il segmento {index} finisce oltre la sorgente", segment.end
                )
            )
    return problems


def _minimum_length(plan: CutPlan, min_segment: float) -> list[Violation]:
    """A segment shorter than this reads as a glitch, not as an edit."""
    return [
        Violation(
            "segmento-troppo-corto",
            f"il segmento {index} dura {segment.duration:.3f}s, minimo {min_segment:.3f}s",
            segment.start,
        )
        for index, segment in enumerate(plan.segments)
        if segment.duration < min_segment - EPSILON
    ]


def _free_edges(plan: CutPlan) -> list[float]:
    """Boundaries a person put inside a word by hand (`manual.Kept.free_*`).
    The engine never gets this exemption: only a hand edit carries the list."""
    return [float(edge) for edge in (plan.manual or {}).get("freeEdges", [])]


def _is_free(position: float, free: list[float]) -> bool:
    return any(abs(position - edge) <= EPSILON for edge in free)


def _never_inside_a_word(plan: CutPlan, analysis: Analysis) -> list[Violation]:
    """No cut lands in the middle of a spoken word.

    This is the rule that protects the voice: a boundary inside a word clips a
    consonant and is audible immediately.
    """
    problems = []
    boundaries = []
    free = _free_edges(plan)
    for index, segment in enumerate(plan.segments):
        boundaries.append((segment.start, f"inizio del segmento {index}"))
        boundaries.append((segment.end, f"fine del segmento {index}"))
    for position, where in boundaries:
        if _is_free(position, free):
            continue
        for word in analysis.words:
            if word.t + EPSILON < position < word.end - EPSILON:
                problems.append(
                    Violation(
                        "taglio-dentro-una-parola",
                        f"{where} cade dentro «{word.s}» ({word.t:.3f}–{word.end:.3f})",
                        position,
                    )
                )
    return problems


def _words_are_whole(plan: CutPlan, analysis: Analysis) -> list[Violation]:
    """Every word is entirely kept or entirely removed, never half of each."""
    problems = []
    free = _free_edges(plan)
    for word in analysis.words:
        if any(word.t < edge < word.end for edge in free):
            continue  # a person split it on purpose
        inside = _overlap_with_segments(word.t, word.end, plan)
        if inside <= EPSILON:
            continue  # fully removed: fine
        if abs(inside - word.duration) > EPSILON:
            problems.append(
                Violation(
                    "parola-spezzata",
                    f"«{word.s}» è tenuta solo per {inside:.3f}s su {word.duration:.3f}s",
                    word.t,
                )
            )
    return problems


def _coverage(plan: CutPlan, analysis: Analysis) -> list[Violation]:
    """Kept plus removed must account for the whole source, with no gap.

    Without this an engine could silently drop a region without recording why,
    which is exactly the kind of invisible decision this design exists to stop.
    """
    spans = [(segment.start, segment.end, "tenuto") for segment in plan.segments]
    spans += [(item.start, item.end, f"rimosso:{item.reason}") for item in plan.removed]
    spans.sort()

    problems = []
    cursor = 0.0
    for start, end, what in spans:
        if start > cursor + EPSILON:
            problems.append(
                Violation(
                    "regione-non-classificata",
                    f"da {cursor:.3f}s a {start:.3f}s non è né tenuto né rimosso",
                    cursor,
                )
            )
        if start < cursor - EPSILON:
            problems.append(
                Violation("regioni-sovrapposte", f"{what} si sovrappone a quanto precede", start)
            )
        cursor = max(cursor, end)
    if cursor < analysis.duration - EPSILON:
        problems.append(
            Violation(
                "regione-non-classificata",
                f"la coda da {cursor:.3f}s a {analysis.duration:.3f}s non è classificata",
                cursor,
            )
        )
    return problems


def _reasons(plan: CutPlan) -> list[Violation]:
    return [
        Violation("motivo-sconosciuto", f"«{item.reason}» non è un motivo previsto", item.start)
        for item in plan.removed
        if item.reason not in REASONS
    ]


# ---------------------------------------------------------------- helpers


def _overlap_with_segments(start: float, end: float, plan: CutPlan) -> float:
    total = 0.0
    for segment in plan.segments:
        total += max(0.0, min(end, segment.end) - max(start, segment.start))
    return total
