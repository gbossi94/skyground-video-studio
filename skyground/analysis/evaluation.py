"""Measuring a proposal against an edit a person actually approved.

The engine was believed to be "roughly right" for a whole day. Measured against
the reference edit it turned out to keep 99.5% of what the editor kept — and to
propose more than twice as much material, because it could not see the restarts
the editor cut. Nobody noticed, because nothing counted.

So: two numbers, and no arguing about them.

* `recall` — how much of the approved edit the proposal preserves. Falling here
  means the engine is throwing away good material, which is the unforgivable
  failure.
* `precision` — how much of the proposal survived into the approved edit. Low
  here means the engine is timid: it leaves in repetitions a person removes.

The unit is the second, not the segment, because a boundary that moves by 200ms
is not an error and should not be counted as one.
"""

from __future__ import annotations

from dataclasses import dataclass

Span = tuple[float, float]


def _merge(spans: list[Span]) -> list[Span]:
    """Overlapping spans counted twice would inflate every number here."""
    merged: list[Span] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _total(spans: list[Span]) -> float:
    return sum(end - start for start, end in spans)


def _intersect(left: list[Span], right: list[Span]) -> list[Span]:
    out: list[Span] = []
    for start, end in left:
        for other_start, other_end in right:
            low, high = max(start, other_start), min(end, other_end)
            if high > low:
                out.append((low, high))
    return _merge(out)


def _subtract(left: list[Span], right: list[Span]) -> list[Span]:
    """What is in `left` and not in `right`."""
    out: list[Span] = []
    for start, end in left:
        pieces = [(start, end)]
        for other_start, other_end in right:
            next_pieces: list[Span] = []
            for piece_start, piece_end in pieces:
                if other_end <= piece_start or other_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if other_start > piece_start:
                    next_pieces.append((piece_start, other_start))
                if other_end < piece_end:
                    next_pieces.append((other_end, piece_end))
            pieces = next_pieces
        out.extend(pieces)
    return _merge(out)


@dataclass(frozen=True)
class Score:
    """How close a proposal came to the approved edit."""

    reference_duration: float
    proposal_duration: float
    shared: float
    #: Kept by the editor and dropped by the engine. The expensive mistake.
    missing: list[Span]
    #: Kept by the engine and dropped by the editor. The visible mistake.
    extra: list[Span]

    @property
    def recall(self) -> float:
        return self.shared / self.reference_duration if self.reference_duration else 0.0

    @property
    def precision(self) -> float:
        return self.shared / self.proposal_duration if self.proposal_duration else 0.0

    @property
    def f1(self) -> float:
        if not (self.recall and self.precision):
            return 0.0
        return 2 * self.recall * self.precision / (self.recall + self.precision)

    def as_dict(self) -> dict:
        return {
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "f1": round(self.f1, 4),
            "referenceDuration": round(self.reference_duration, 2),
            "proposalDuration": round(self.proposal_duration, 2),
            "sharedDuration": round(self.shared, 2),
            "missingDuration": round(_total(self.missing), 2),
            "extraDuration": round(_total(self.extra), 2),
        }

    def summary(self) -> str:
        return (
            f"recupero {self.recall * 100:.1f}% · precisione {self.precision * 100:.1f}% · "
            f"proposta {self.proposal_duration:.1f}s contro {self.reference_duration:.1f}s"
        )


def compare(proposal: list[Span], reference: list[Span], *, tolerance: float = 0.25) -> Score:
    """Score `proposal` against the approved `reference`.

    `tolerance` forgives the edges: a cut placed 200ms early is a different
    breath, not a different edit, and counting it as an error would drown the
    real differences in noise.
    """
    proposed = _merge(list(proposal))
    approved = _merge(list(reference))
    shared = _total(_intersect(proposed, approved))

    def significant(spans: list[Span]) -> list[Span]:
        return [(start, end) for start, end in spans if end - start > tolerance]

    return Score(
        reference_duration=_total(approved),
        proposal_duration=_total(proposed),
        shared=shared,
        missing=significant(_subtract(approved, proposed)),
        extra=significant(_subtract(proposed, approved)),
    )


def describe(spans: list[Span], words, limit: int = 12) -> list[str]:
    """What is actually inside these spans, longest first — so a number becomes
    something you can read and argue with."""
    lines = []
    for start, end in sorted(spans, key=lambda span: span[1] - span[0], reverse=True)[:limit]:
        said = " ".join(word.s for word in words if word.t >= start and word.end <= end)
        lines.append(f"{start:7.2f}–{end:7.2f} ({end - start:5.1f}s)  {said[:100]}")
    return lines
