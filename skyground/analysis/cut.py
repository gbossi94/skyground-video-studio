"""The cut engine.

A pure function: `plan_cut(analysis, policy, decisions)` always returns the same
plan for the same inputs. Answering a question is not an edit to the plan, it is
another input — which means a plan can always be rebuilt from the analysis plus
the list of human decisions, and the reasoning stays inspectable forever.

The engine only removes what it can justify:

* a pause is removed because it is silence, and how much is kept is policy;
* an attempt is removed because a better attempt at the same line exists *and*
  the margin between them was wide enough to decide without asking;
* everything else stays, and anything suspicious becomes a question.

The consequence is that the first proposal is usually longer than a human edit.
That is the intended trade: the engine never shortens the video by guessing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from skyground.analysis import align, takes
from skyground.analysis.models import (
    ASK_PAUSE_INTENT,
    ASK_TAKE_CHOICE,
    REASON_FILLER,
    REASON_LEAD_IN,
    REASON_LEAD_OUT,
    REASON_RETAKE,
    REASON_SILENCE,
    Analysis,
    CutPlan,
    Option,
    Question,
    Removed,
    Segment,
    Utterance,
)


@dataclass(frozen=True)
class CutPolicy:
    """Every number the engine is allowed to have an opinion about."""

    #: A gap this long starts a new utterance.
    utterance_gap: float = 0.55
    #: A pause longer than this is trimmed…
    max_pause: float = 0.60
    #: …down to this much, so the edit still breathes.
    keep_pause: float = 0.22
    #: Air kept before the first word and after the last of a segment.
    lead_in: float = 0.12
    lead_out: float = 0.28
    #: Shorter than this is a glitch, not a cut.
    min_segment: float = 0.35
    #: Two utterances this similar are certainly the same line…
    certain_similarity: float = 0.60
    #: …and this similar are worth asking about.
    suspect_similarity: float = 0.30
    #: Below this score margin the engine refuses to pick a take on its own.
    decide_margin: float = 0.22
    #: A mid-sentence pause longer than this may be deliberate: ask.
    rhetorical_pause: float = 1.20

    def as_dict(self) -> dict:
        return asdict(self)


def plan_cut(
    analysis: Analysis,
    policy: CutPolicy | None = None,
    decisions: dict[str, str] | None = None,
    *,
    suspects: list[dict] | None = None,
    now: str | None = None,
) -> CutPlan:
    """Propose an edit of `analysis`, honouring the decisions already made.

    `suspects` carries extra retake candidates found by something the engine
    cannot do itself — an adviser reading the transcript for meaning rather than
    for words. They only ever become questions.
    """
    policy = policy or CutPolicy()
    decisions = dict(decisions or {})
    # Whisper's word ends run into the pauses that follow them, and every cut
    # here is decided by looking for gaps between words: a pause hidden inside
    # an inflated word is a pause this engine cannot see. The audio measured its
    # own silence, and where the two disagree the audio is right.
    #
    # Corrected once, into the analysis itself, so that nothing downstream can
    # reach past it to the original timings — which is exactly the mistake that
    # made the first attempt at this fix do nothing at all. Idempotent, so a
    # caller that already prepared the analysis (the adviser needs it to, in
    # order to number the same utterances this function will build) loses
    # nothing by it.
    analysis = align.prepare(analysis)
    words = analysis.words

    plan = CutPlan(
        source=analysis.source,
        source_duration=analysis.duration,
        policy=policy.as_dict(),
        generated_at=now or datetime.now(UTC).isoformat(timespec="seconds"),
    )
    if not words:
        if analysis.duration > 0:
            plan.removed.append(
                Removed(0.0, analysis.duration, REASON_SILENCE, 1.0, "nessun parlato rilevato")
            )
        return plan

    utterances = takes.build_utterances(words, gap=policy.utterance_gap)
    plan.utterances = utterances

    groups = takes.group_takes(
        utterances, threshold=policy.certain_similarity, lookahead=4
    )
    plan.takes = groups

    dropped: dict[int, str] = {}  # utterance index -> why

    # ------------------------------------------------------- repeated attempts
    for group in groups:
        best, margin, scores = takes.rank_group(group, utterances, words)
        group.chosen = best
        group.margin = margin
        group.scores = {index: score.total for index, score in scores.items()}

        question_id = _take_question_id(group.utterances)
        answer = decisions.get(question_id)

        if answer is not None:
            # A decision already taken stays in the plan, marked as taken. It
            # used to be consumed and forgotten: the rebuilt plan carried its
            # effect with no trace of the choice, so nobody could see what had
            # been decided, or change their mind.
            decided = _take_question(
                question_id, group.utterances, utterances, scores, best, margin
            )
            decided.answer = answer
            plan.questions.append(decided)

            if answer == "keep-both":
                # Not a retake after all: every attempt stays in the edit.
                group.chosen = None
                continue
            chosen = _utterance_from_answer(answer, group.utterances)
            group.chosen = chosen
            for index in group.utterances:
                if index != chosen:
                    dropped[index] = "scelta dell'editor"
            continue

        if margin >= policy.decide_margin:
            # Wide enough to be a fact rather than a preference.
            for index in group.utterances:
                if index != best:
                    dropped[index] = f"ripetizione superata (margine {margin:.2f})"
            continue

        # Too close to call: keep every attempt and ask.
        plan.questions.append(
            _take_question(question_id, group.utterances, utterances, scores, best, margin)
        )

    # ------------------------------------------------ suspected reformulations
    for suspect in suspects or []:
        first, second = int(suspect["a"]), int(suspect["b"])
        question_id = _take_question_id([first, second])
        if question_id in {question.id for question in plan.questions}:
            continue
        answer = decisions.get(question_id)
        question = _suspect_question(
            question_id, first, second, utterances, suspect.get("why", ""), words
        )
        question.answer = answer
        plan.questions.append(question)
        if answer is None or answer == "keep-both":
            continue
        chosen = _utterance_from_answer(answer, [first, second])
        for index in (first, second):
            if index != chosen:
                dropped[index] = "scelta dell'editor"

    for index, reason in dropped.items():
        utterances[index].kept = False
        utterances[index].drop_reason = reason

    # ------------------------------------------------ run-ups said twice over
    # A person talking to camera stumbles far more often than they re-shoot a
    # whole line, and the stumble happens inside a single breath. Comparing
    # utterances to each other cannot see it; this can.
    kept_words = _kept_word_flags(utterances, len(words))
    restarts = takes.find_restarts(utterances, words) + takes.find_abandoned_starts(utterances)
    for restart in restarts:
        if not any(kept_words[restart.first_word : restart.last_word + 1]):
            continue  # already gone with its utterance
        question_id = f"restart:{restart.first_word}-{restart.last_word}"
        answer = decisions.get(question_id)
        if answer is None and restart.certain:
            _drop_words(kept_words, restart)
            plan.restarts.append(restart_as_dict(restart, words))
            continue
        question = _restart_question(question_id, restart, words)
        question.answer = answer
        plan.questions.append(question)
        if answer == "cut":
            _drop_words(kept_words, restart)
            plan.restarts.append(restart_as_dict(restart, words))

    # ----------------------------------------------------------- the segments
    plan.segments = _build_segments(analysis, utterances, kept_words, policy, plan, decisions)
    plan.removed = _classify_removed(analysis, plan.segments, utterances, dropped)

    plan.questions.sort(key=lambda question: question.at)
    return plan


# --------------------------------------------------------------------- pieces


def _drop_words(kept: list[bool], restart: takes.Restart) -> None:
    for index in range(restart.first_word, restart.last_word + 1):
        kept[index] = False


def restart_as_dict(restart: takes.Restart, words: list) -> dict:
    return {
        "kind": restart.kind,
        "start": round(words[restart.first_word].t, 3),
        "end": round(words[restart.last_word].end, 3),
        "detail": restart.detail,
        "words": restart.word_count,
    }


def _restart_question(question_id: str, restart: takes.Restart, words: list) -> Question:
    said = " ".join(word.s for word in words[restart.first_word : restart.last_word + 1])
    start, end = words[restart.first_word].t, words[restart.last_word].end
    return Question(
        id=question_id,
        kind=ASK_TAKE_CHOICE,
        at=start,
        prompt="Questa ripartenza si taglia?",
        context=f"{restart.detail}. Il pezzo che toglierei è: «{said[:120]}»",
        options=[
            Option(
                id="cut",
                label="Togliere il primo tentativo",
                detail=f"{end - start:.1f}s · resta la ripresa buona",
                recommended=True,
                start=start,
                end=end,
            ),
            Option(
                id="keep",
                label="Tenere tutto",
                detail="La ripetizione è voluta.",
                start=max(0.0, start - 1.0),
                end=end + 2.0,
            ),
        ],
    )


def _kept_word_flags(utterances: list[Utterance], total: int) -> list[bool]:
    kept = [False] * total
    for utterance in utterances:
        if not utterance.kept:
            continue
        for index in range(utterance.first_word, utterance.last_word + 1):
            kept[index] = True
    return kept


def _runs(kept: list[bool]) -> list[tuple[int, int]]:
    runs, start = [], None
    for index, flag in enumerate(kept):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(kept) - 1))
    return runs


def _build_segments(
    analysis: Analysis,
    utterances: list[Utterance],
    kept: list[bool],
    policy: CutPolicy,
    plan: CutPlan,
    decisions: dict[str, str],
) -> list[Segment]:
    """Turn runs of kept words into segments, trimming the pauses inside them.

    Padding never reaches past a neighbouring word: that single clamp is what
    guarantees no cut lands inside speech and no discarded take leaks back in.
    """
    words = analysis.words
    segments: list[Segment] = []

    for first, last in _runs(kept):
        # Split the run wherever the speaker pauses longer than the policy allows.
        pieces: list[tuple[int, int]] = []
        piece_start = first
        for index in range(first, last):
            gap = words[index + 1].t - words[index].end
            if gap <= policy.max_pause:
                continue
            if gap >= policy.rhetorical_pause and not _ends_sentence(words[index].s):
                question = _pause_question(words[index].end, words[index + 1].t, gap, words, index)
                # Recorded either way: open when nobody has ruled on it, marked
                # with the answer when somebody has. A decision that disappears
                # from the plan cannot be reviewed or taken back.
                question.answer = decisions.get(question.id)
                plan.questions.append(question)
                if question.answer == "keep":
                    continue  # the editor called it deliberate: leave it whole
            pieces.append((piece_start, index))
            piece_start = index + 1
        pieces.append((piece_start, last))

        for piece_first, piece_last in pieces:
            segment = _segment_for(words, piece_first, piece_last, analysis.duration, policy)
            if segment.duration < policy.min_segment:
                # Too short to stand alone: glue it to the previous segment when
                # they are adjacent in the source, otherwise leave it out and let
                # the removal be recorded with a reason.
                if segments and abs(segments[-1].end - segment.start) < policy.keep_pause * 2:
                    segments[-1] = Segment(
                        segments[-1].start,
                        segment.end,
                        segments[-1].label,
                        segments[-1].first_word,
                        segment.last_word,
                    )
                    continue
            segment.label = _label_for(utterances, piece_first)
            segments.append(segment)

    return _merge_touching(segments)


def _segment_for(words, first: int, last: int, duration: float, policy: CutPolicy) -> Segment:
    previous_end = words[first - 1].end if first > 0 else 0.0
    following_start = words[last + 1].t if last + 1 < len(words) else duration
    start = max(previous_end, words[first].t - policy.lead_in, 0.0)
    end = min(following_start, words[last].end + policy.lead_out, duration)
    return Segment(start=start, end=end, first_word=first, last_word=last)


def _merge_touching(segments: list[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    for segment in segments:
        if merged and abs(segment.start - merged[-1].end) < 1e-6:
            merged[-1] = Segment(
                merged[-1].start,
                segment.end,
                merged[-1].label,
                merged[-1].first_word,
                segment.last_word,
            )
            continue
        merged.append(segment)
    return merged


def _classify_removed(
    analysis: Analysis,
    segments: list[Segment],
    utterances: list[Utterance],
    dropped: dict[int, str],
) -> list[Removed]:
    """Everything outside the segments, each piece with a stated reason."""
    removed: list[Removed] = []
    cursor = 0.0
    boundaries = [(segment.start, segment.end) for segment in segments] + [
        (analysis.duration, analysis.duration)
    ]
    for start, end in boundaries:
        if start > cursor + 1e-9:
            removed.append(_describe_gap(analysis, cursor, start, utterances, dropped))
        cursor = max(cursor, end)
    return [item for item in removed if item.duration > 1e-6]


def _describe_gap(
    analysis: Analysis,
    start: float,
    end: float,
    utterances: list[Utterance],
    dropped: dict[int, str],
) -> Removed:
    inside = [
        utterance
        for utterance in utterances
        if utterance.start < end - 1e-6 and utterance.end > start + 1e-6
    ]
    spoken = [utterance for utterance in inside if not utterance.kept]

    if spoken:
        detail = "; ".join(
            f"#{utterance.index} {dropped.get(utterance.index, 'scartata')}: "
            f"«{utterance.text[:60]}»"
            for utterance in spoken
        )
        reason = REASON_FILLER if all(_is_filler(u.text) for u in spoken) else REASON_RETAKE
        return Removed(start, end, reason, 0.9, detail)

    if start <= 1e-6:
        return Removed(start, end, REASON_LEAD_IN, 1.0, "silenzio prima della prima battuta")
    if end >= analysis.duration - 1e-6:
        return Removed(start, end, REASON_LEAD_OUT, 1.0, "silenzio dopo l'ultima battuta")
    return Removed(start, end, REASON_SILENCE, 1.0, f"pausa di {end - start:.2f}s")


def _is_filler(text: str) -> bool:
    words = takes.tokens(text)
    return bool(words) and all(word in takes.FILLERS for word in words)


def _ends_sentence(word: str) -> bool:
    return bool(takes.SENTENCE_END.search(word.strip()))


def _label_for(utterances: list[Utterance], word_index: int) -> str:
    for utterance in utterances:
        if utterance.first_word <= word_index <= utterance.last_word:
            return utterance.text[:60]
    return ""


# ------------------------------------------------------------------ questions


def _take_question_id(indices: list[int]) -> str:
    return "take:" + "-".join(str(index) for index in sorted(indices))


def _utterance_from_answer(answer: str, indices: list[int]) -> int:
    """Answers name an utterance (`utterance:7`); anything else keeps the first."""
    if answer.startswith("utterance:"):
        try:
            chosen = int(answer.split(":", 1)[1])
        except ValueError:
            return indices[0]
        return chosen if chosen in indices else indices[0]
    return indices[0]


def _take_question(
    question_id: str,
    indices: list[int],
    utterances: list[Utterance],
    scores,
    best: int,
    margin: float,
) -> Question:
    options = []
    for index in indices:
        utterance = utterances[index]
        detail = (
            f"{utterance.start:.2f}–{utterance.end:.2f}s · {utterance.duration:.1f}s · "
            f"punteggio {scores[index].total:.2f}"
        )
        options.append(
            Option(
                id=f"utterance:{index}",
                label=utterance.text[:90],
                detail=detail,
                recommended=(index == best),
                start=utterance.start,
                end=utterance.end,
            )
        )
    options.append(
        Option(
            id="keep-both",
            label="Tenerle entrambe",
            detail="Nessuna è una ripetizione: restano tutte nel montaggio.",
        )
    )
    return Question(
        id=question_id,
        kind=ASK_TAKE_CHOICE,
        at=utterances[indices[0]].start,
        prompt="Quale di queste take vuoi tenere?",
        context=(
            f"Sembrano {len(indices)} tentativi della stessa battuta, e i punteggi sono vicini "
            f"(margine {margin:.2f}): la scelta cambia il senso, quindi non la faccio io."
        ),
        options=options,
    )


def _suspect_question(
    question_id: str,
    first: int,
    second: int,
    utterances: list[Utterance],
    why: str,
    words: list | None = None,
) -> Question:
    # The recommendation used to be "the second one, always" — a rule this
    # project knows to be wrong, and says so in `takes`: on the reference
    # footage the editor kept the *first* attempt of the opening line. Blindly
    # preferring the later take cost six seconds of approved material on one
    # question alone. So these are scored like any other pair, and when the two
    # are too close to separate, neither is marked.
    pair = [utterances[first], utterances[second]]
    if words:
        scores = [takes.score_take(item, words, pair, index) for index, item in enumerate(pair)]
        better = 1 if scores[1].total > scores[0].total else 0
        margin = abs(scores[1].total - scores[0].total)
    else:  # pragma: no cover - only when a caller has no transcript to hand
        better, margin = 1, 0.0
    confident = margin >= 0.08

    options = [
        Option(
            id=f"utterance:{second}",
            label=f"Tenere la seconda: {utterances[second].text[:80]}",
            detail=f"{utterances[second].start:.2f}–{utterances[second].end:.2f}s",
            recommended=confident and better == 1,
            start=utterances[second].start,
            end=utterances[second].end,
        ),
        Option(
            id=f"utterance:{first}",
            label=f"Tenere la prima: {utterances[first].text[:80]}",
            detail=f"{utterances[first].start:.2f}–{utterances[first].end:.2f}s",
            recommended=confident and better == 0,
            start=utterances[first].start,
            end=utterances[first].end,
        ),
        Option(id="keep-both", label="Tenerle entrambe", detail="Dicono cose diverse."),
    ]
    return Question(
        id=question_id,
        kind=ASK_TAKE_CHOICE,
        at=utterances[first].start,
        prompt="Queste due battute dicono la stessa cosa?",
        context=why or "Sembrano due formulazioni dello stesso concetto, ma con parole diverse.",
        options=options,
    )


def _pause_question(start: float, end: float, gap: float, words, index: int) -> Question:
    before = " ".join(word.s for word in words[max(0, index - 5) : index + 1])
    after = " ".join(word.s for word in words[index + 1 : index + 6])
    return Question(
        id=f"pause:{start:.3f}",
        kind=ASK_PAUSE_INTENT,
        at=start,
        prompt=f"Questa pausa di {gap:.1f}s è voluta?",
        context=(
            f"…{before} ⟨pausa⟩ {after}… La frase non era conclusa, "
            "quindi potrebbe essere una pausa retorica."
        ),
        options=[
            Option(
                id="cut",
                label="Tagliarla",
                detail="Resta il respiro minimo previsto dalla policy.",
                recommended=True,
                # Entrambe le opzioni riguardano lo stesso silenzio, con un po'
                # di parlato intorno: senza contesto una pausa non si giudica.
                start=max(0.0, start - 2.0),
                end=end + 2.0,
            ),
            Option(
                id="keep",
                label="Tenerla intera",
                detail="È un silenzio voluto, fa parte della battuta.",
                start=max(0.0, start - 2.0),
                end=end + 2.0,
            ),
        ],
    )
