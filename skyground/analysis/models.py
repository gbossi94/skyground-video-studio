"""Data model of the analysis and of a cut plan.

Standard library only, and deliberately free of any I/O: everything here is a
plain value that can be serialised to JSON, stored as a project document and
replayed. The cut engine is a pure function over these types, which is what
makes it testable against the real footage without touching a media file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1


# --------------------------------------------------------------------- reasons

#: Why a region of the source does not reach the edit.
REASON_SILENCE = "silence"  # a pause between words
REASON_LEAD_IN = "lead-in"  # dead air before the first word of the take
REASON_LEAD_OUT = "lead-out"  # dead air after the last word
REASON_FILLER = "filler"  # a standalone hesitation
REASON_RETAKE = "retake"  # an attempt superseded by a better one
REASON_MANUAL = "manual"  # removed because a person said so
REASONS = (
    REASON_SILENCE,
    REASON_LEAD_IN,
    REASON_LEAD_OUT,
    REASON_FILLER,
    REASON_RETAKE,
    REASON_MANUAL,
)

# ------------------------------------------------------------------- questions

#: Kinds of ambiguity. Every one of them blocks the plan until a person answers.
ASK_TAKE_CHOICE = "take-choice"  # several attempts, no clear winner
ASK_PAUSE_INTENT = "pause-intent"  # a long pause that may be deliberate
ASK_FILLER_INSIDE = "filler-inside"  # a hesitation glued to the sentence around it
ASK_BOUNDARY = "boundary"  # not enough silence to cut cleanly
ASK_LOW_CONFIDENCE = "low-confidence"  # the transcript is unsure exactly here
ASK_KINDS = (
    ASK_TAKE_CHOICE,
    ASK_PAUSE_INTENT,
    ASK_FILLER_INSIDE,
    ASK_BOUNDARY,
    ASK_LOW_CONFIDENCE,
)


@dataclass(frozen=True)
class Word:
    """One spoken word with its position in the source."""

    t: float
    end: float
    s: str
    p: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.t)

    def as_dict(self) -> dict:
        return {
            "t": round(self.t, 3),
            "end": round(self.end, 3),
            "s": self.s,
            "p": round(self.p, 3),
        }

    @staticmethod
    def from_dict(value: dict) -> Word:
        return Word(
            t=float(value["t"]),
            end=float(value["end"]),
            s=str(value["s"]),
            p=float(value.get("p", 1.0)),
        )


@dataclass(frozen=True)
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3)}

    @staticmethod
    def from_dict(value: dict) -> Silence:
        return Silence(start=float(value["start"]), end=float(value["end"]))


@dataclass
class Analysis:
    """Everything the engine knows about the source, and nothing it decided."""

    source: str
    duration: float
    words: list[Word] = field(default_factory=list)
    silences: list[Silence] = field(default_factory=list)
    language: str = "it"
    provider: str = ""
    generated_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {
            "schemaVersion": self.schema_version,
            "source": self.source,
            "duration": round(self.duration, 3),
            "language": self.language,
            "provider": self.provider,
            "generatedAt": self.generated_at,
            "words": [word.as_dict() for word in self.words],
            "silences": [silence.as_dict() for silence in self.silences],
        }

    @staticmethod
    def from_dict(value: dict) -> Analysis:
        return Analysis(
            source=value.get("source", ""),
            duration=float(value.get("duration", 0.0)),
            words=[Word.from_dict(item) for item in value.get("words", [])],
            silences=[Silence.from_dict(item) for item in value.get("silences", [])],
            language=value.get("language", "it"),
            provider=value.get("provider", ""),
            generated_at=value.get("generatedAt", ""),
            schema_version=int(value.get("schemaVersion", SCHEMA_VERSION)),
        )


@dataclass
class Utterance:
    """A run of words with no long pause inside it: one attempt at saying something."""

    index: int
    first_word: int
    last_word: int
    start: float
    end: float
    text: str
    #: Set when this utterance belongs to a group of repeated attempts.
    take_group: int | None = None
    kept: bool = True
    drop_reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def word_count(self) -> int:
        return self.last_word - self.first_word + 1

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "firstWord": self.first_word,
            "lastWord": self.last_word,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "takeGroup": self.take_group,
            "kept": self.kept,
            "dropReason": self.drop_reason,
        }


@dataclass
class TakeGroup:
    """Several attempts at the same line. Exactly one of them survives."""

    id: int
    utterances: list[int]
    chosen: int | None = None
    scores: dict[int, float] = field(default_factory=dict)
    #: Distance between the best and the runner-up. A small margin means "ask".
    margin: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "utterances": list(self.utterances),
            "chosen": self.chosen,
            "scores": {str(key): round(value, 4) for key, value in self.scores.items()},
            "margin": round(self.margin, 4),
        }


@dataclass
class Segment:
    """A piece of the source that reaches the edit."""

    start: float
    end: float
    label: str = ""
    first_word: int | None = None
    last_word: int | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "label": self.label,
            "firstWord": self.first_word,
            "lastWord": self.last_word,
        }

    @staticmethod
    def from_dict(value: dict) -> Segment:
        return Segment(
            start=float(value["start"]),
            end=float(value["end"]),
            label=value.get("label", ""),
            first_word=value.get("firstWord"),
            last_word=value.get("lastWord"),
        )


@dataclass
class Removed:
    """A piece of the source that does not, and why."""

    start: float
    end: float
    reason: str
    confidence: float = 1.0
    detail: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "detail": self.detail,
        }

    @staticmethod
    def from_dict(value: dict) -> Removed:
        return Removed(
            start=float(value["start"]),
            end=float(value["end"]),
            reason=value["reason"],
            confidence=float(value.get("confidence", 1.0)),
            detail=value.get("detail", ""),
        )


@dataclass
class Option:
    """One way of resolving a question, with the consequence spelled out."""

    id: str
    label: str
    detail: str = ""
    #: Marked as what the engine would do, but never applied on its own.
    recommended: bool = False
    #: The piece of source this option is about, when it is about one. Nobody
    #: can choose between two takes by reading them: the interface needs the
    #: range in order to play it, and a timestamp written inside `detail` is
    #: prose, not data.
    start: float | None = None
    end: float | None = None

    def as_dict(self) -> dict:
        payload = {
            "id": self.id,
            "label": self.label,
            "detail": self.detail,
            "recommended": self.recommended,
        }
        if self.start is not None and self.end is not None:
            payload["start"] = round(self.start, 3)
            payload["end"] = round(self.end, 3)
        return payload

    @staticmethod
    def from_dict(value: dict) -> Option:
        return Option(
            id=value["id"],
            label=value["label"],
            detail=value.get("detail", ""),
            recommended=bool(value.get("recommended", False)),
            start=value.get("start"),
            end=value.get("end"),
        )


@dataclass
class Question:
    """An ambiguity the engine refuses to resolve by itself.

    A plan with an unanswered question cannot be applied. This is the mechanical
    form of the rule that the edit must never guess.
    """

    id: str
    kind: str
    at: float
    prompt: str
    options: list[Option] = field(default_factory=list)
    answer: str | None = None
    answered_by: str | None = None
    #: Free text the engine wants the person to see before choosing.
    context: str = ""

    @property
    def resolved(self) -> bool:
        return self.answer is not None

    def option(self, option_id: str) -> Option | None:
        for option in self.options:
            if option.id == option_id:
                return option
        return None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "at": round(self.at, 3),
            "prompt": self.prompt,
            "context": self.context,
            "options": [option.as_dict() for option in self.options],
            "answer": self.answer,
            "answeredBy": self.answered_by,
            "resolved": self.resolved,
        }

    @staticmethod
    def from_dict(value: dict) -> Question:
        return Question(
            id=value["id"],
            kind=value["kind"],
            at=float(value.get("at", 0.0)),
            prompt=value.get("prompt", ""),
            context=value.get("context", ""),
            options=[Option.from_dict(item) for item in value.get("options", [])],
            answer=value.get("answer"),
            answered_by=value.get("answeredBy"),
        )


STATUS_DRAFT = "draft"
STATUS_READY = "ready"  # every question answered
STATUS_APPLIED = "applied"


@dataclass
class CutPlan:
    """A proposed edit: what stays, what goes, and what still has to be decided."""

    source: str
    source_duration: float
    segments: list[Segment] = field(default_factory=list)
    removed: list[Removed] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    utterances: list[Utterance] = field(default_factory=list)
    takes: list[TakeGroup] = field(default_factory=list)
    #: Run-ups removed from inside an utterance — the stumble-and-start-again
    #: that is most of what an editor actually cuts. Kept so the reasoning is
    #: readable afterwards, like every other removal.
    restarts: list[dict] = field(default_factory=list)
    #: Whole stretches of script delivered twice, and which delivery won. A
    #: passage is the unit an editor actually chooses between; comparing single
    #: lines left two thirds of every discarded attempt in the edit.
    passages: list[dict] = field(default_factory=list)
    #: What the editor model said about its own work: which model ran, its
    #: summary of the film, the repairs made to its answer, and what the
    #: re-reading found. Empty when the heuristic engine made the plan.
    editor: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    applied_at: str = ""
    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------ state

    @property
    def open_questions(self) -> list[Question]:
        return [question for question in self.questions if not question.resolved]

    @property
    def status(self) -> str:
        if self.applied_at:
            return STATUS_APPLIED
        return STATUS_DRAFT if self.open_questions else STATUS_READY

    @property
    def output_duration(self) -> float:
        return sum(segment.duration for segment in self.segments)

    @property
    def removed_duration(self) -> float:
        return sum(item.duration for item in self.removed)

    def stats(self) -> dict:
        source = self.source_duration or 0.0
        output = self.output_duration
        return {
            "sourceDuration": round(source, 3),
            "outputDuration": round(output, 3),
            "removedDuration": round(source - output, 3),
            "removedShare": round((source - output) / source, 4) if source else 0.0,
            "segments": len(self.segments),
            "openQuestions": len(self.open_questions),
            "questions": len(self.questions),
        }

    # ------------------------------------------------------------ persistence

    def as_dict(self) -> dict:
        return {
            "schemaVersion": self.schema_version,
            "source": self.source,
            "sourceDuration": round(self.source_duration, 3),
            "status": self.status,
            "generatedAt": self.generated_at,
            "appliedAt": self.applied_at,
            "policy": dict(self.policy),
            "stats": self.stats(),
            "segments": [segment.as_dict() for segment in self.segments],
            "removed": [item.as_dict() for item in self.removed],
            "questions": [question.as_dict() for question in self.questions],
            "utterances": [utterance.as_dict() for utterance in self.utterances],
            "takes": [take.as_dict() for take in self.takes],
            "restarts": list(self.restarts),
            "passages": list(self.passages),
            "editor": dict(self.editor),
        }

    @staticmethod
    def from_dict(value: dict) -> CutPlan:
        plan = CutPlan(
            source=value.get("source", ""),
            source_duration=float(value.get("sourceDuration", 0.0)),
            segments=[Segment.from_dict(item) for item in value.get("segments", [])],
            removed=[Removed.from_dict(item) for item in value.get("removed", [])],
            questions=[Question.from_dict(item) for item in value.get("questions", [])],
            policy=dict(value.get("policy", {})),
            generated_at=value.get("generatedAt", ""),
            applied_at=value.get("appliedAt", ""),
            schema_version=int(value.get("schemaVersion", SCHEMA_VERSION)),
        )
        for item in value.get("utterances", []):
            plan.utterances.append(
                Utterance(
                    index=item["index"],
                    first_word=item["firstWord"],
                    last_word=item["lastWord"],
                    start=float(item["start"]),
                    end=float(item["end"]),
                    text=item.get("text", ""),
                    take_group=item.get("takeGroup"),
                    kept=bool(item.get("kept", True)),
                    drop_reason=item.get("dropReason", ""),
                )
            )
        plan.restarts = list(value.get("restarts", []))
        plan.passages = list(value.get("passages", []))
        plan.editor = dict(value.get("editor", {}) or {})
        for item in value.get("takes", []):
            plan.takes.append(
                TakeGroup(
                    id=item["id"],
                    utterances=list(item.get("utterances", [])),
                    chosen=item.get("chosen"),
                    scores={int(key): float(val) for key, val in item.get("scores", {}).items()},
                    margin=float(item.get("margin", 0.0)),
                )
            )
        return plan

    def to_timeline_clips(self) -> list[dict]:
        """Render the plan in the shape `timeline.json` already uses."""
        clips = []
        cursor = 0.0
        for index, segment in enumerate(self.segments):
            clips.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "label": segment.label or f"Segmento {index + 1}",
                    "output_start": cursor,
                    "original_segment": index,
                }
            )
            cursor += segment.duration
        return clips
