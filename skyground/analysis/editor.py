"""The editor: the model decides what is said, the code decides where to cut.

For a week the decision was made the other way round. A string comparison
looked for repeated lines, a hand-tuned score chose between them, and a model
was allowed to *suggest* pairs the comparison had missed. On the reference
footage that machine kept two attempts at the opening line back to back —
«sei nel famosissimo fango. 20 mila euro al mese, sei in quello che io chiamo
il fango» — because the speaker had restarted mid-sentence and the rule
demanded a shared opening. Any reader sees it in a second. A comparison of
strings cannot see it, ever, by construction. And the metric said 90% precision
while it was in the film.

So the responsibilities are split along what each side can actually do. The
model reads the whole transcript — a few thousand tokens, the entire video in
one look, which no human editor gets — and says what stays, with a reason for
every cut. The code does what it is good at and the model is not: it knows to
the millisecond where every word begins, where the silence is, and where a cut
can land without touching speech; it enforces the invariants; and it reads the
result back to the model for a second opinion before anything is rendered.

Nothing in here scores a take. Nothing in here has a similarity threshold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from skyground.analysis import align, takes
from skyground.analysis.cut import ENGINE, CutPolicy, _build_segments, _classify_removed
from skyground.analysis.models import Analysis, CutPlan, Option, Question, Word
from skyground.errors import ConfigurationError

#: The air the editor leaves. A join is four tenths of a second — a clause
#: pause, the length a speaker takes between two sentences — and a pause the
#: speaker made is left alone up to nearly that, so the rhythm stays theirs.
#: The first version squeezed every join to 0.20s and split every pause over
#: 0.40: three natural pauses in a row came out as a burst of machine-gun
#: cuts, and a lead-out of 0.14 clipped the tails of words. Both were heard.
EDITOR_POLICY = CutPolicy(
    max_pause=0.45,
    keep_pause=0.18,
    lead_in=0.10,
    lead_out=0.30,
    min_segment=0.35,
    rhetorical_pause=99.0,  # no pause is called deliberate by a number
    ask_when_unsure=False,
)

#: A gap this long in the result is a hole, and goes to the reviewer.
HOLE = 0.50
#: Five words in a row said twice is content said twice, unless it is a refrain
#: — which is for the reviewer to say, not for a number.
REPEAT = 5
#: How many times the reviewer may send the edit back.
REVIEW_ROUNDS = 2

ASK_EDIT = "take-choice"


# ------------------------------------------------------------- the transcript


def present(words: list[Word], utterances: list[takes.Utterance]) -> str:
    """The transcript as the model reads it: every word numbered, in breaths.

    Numbering every word is noisy to look at and is the only thing that makes a
    decision unambiguous — «tieni da "o scommetti" in poi» points at three
    places in this footage; «tieni da 238» points at one.
    """
    lines = []
    for utterance in utterances:
        lines.append(f"#{utterance.index} ({utterance.start:.2f}–{utterance.end:.2f}s)")
        lines.append(
            "  " + " ".join(
                f"{index}:{words[index].s}"
                for index in range(utterance.first_word, utterance.last_word + 1)
            )
        )
    return "\n".join(lines)


def present_result(words: list[Word], kept: list[bool], segments) -> str:
    """The edit as it would be heard, with the seams shown.

    The reviewer needs to see the cuts, not only what survived: a sentence that
    starts mid-thought is invisible in running text and obvious next to a
    ⟨taglio⟩ mark. Original word numbers are kept so a revision can point back.
    """
    joins = {segment.first_word for segment in segments[1:]}
    out, line = [], []
    previous = None
    for index, word in enumerate(words):
        if not kept[index]:
            continue
        if previous is not None and index != previous + 1:
            line.append("⟨taglio⟩")
        if index in joins and previous is not None and index == previous + 1:
            line.append("⟨pausa tolta⟩")
        line.append(f"{index}:{word.s}")
        previous = index
        if len(line) >= 14 or re.search(r"[.!?]$", word.s):
            out.append(" ".join(line))
            line = []
    if line:
        out.append(" ".join(line))
    return "\n".join(out)


# ---------------------------------------------------------------- the model


DECIDE = """Sei il montatore di Skyground. Ricevi la trascrizione, parola per parola e con i tempi, di un girato in cui una persona parla a camera. La persona inciampa, ricomincia, dice la stessa cosa più volte e a un certo punto la dice bene.

Il tuo lavoro è decidere cosa resta nel video. Per ogni cosa che la persona voleva dire tieni la versione migliore — una sola — e togli tutto il resto: tentativi abbandonati, ripartenze, ripetizioni, riformulazioni della stessa idea, intercalari isolati, false partenze. Il risultato deve scorrere come se fosse stato detto bene la prima volta.

La versione migliore è quella completa, fluida, detta con convinzione. Non è necessariamente l'ultima. Quando un tentativo si ferma e riparte, la parte buona di solito comincia dopo la ripartenza, anche a metà di un respiro: taglia lì. Tieni frasi intere — mai una clip che comincia a metà pensiero o finisce prima del punto.

Non cambiare l'ordine. Non tenere due volte lo stesso contenuto, anche se le parole sono diverse. Se un pezzo è unico ma lungo, tienilo: togliere contenuto per ritmo è una scelta di chi pubblica, non tua. Se un pezzo è inutilizzabile — frase mai finita e senza una versione buona — toglilo e dillo nel motivo.

Rispondi con segmenti contigui che coprono ogni parola da 0 all'ultima, ognuno keep o cut, con un motivo breve e concreto («seconda ripresa di #12, la prima si ferma a "vorresti"»). Il campo quote riporta la prima e l'ultima parola del segmento, per controllo."""

REVIEW = """Sei il montatore di Skyground e stai rileggendo un montaggio prima che venga renderizzato. Ricevi il testo del video come uscirebbe, con ⟨taglio⟩ dove due pezzi non contigui sono stati incollati, e sotto le segnalazioni dei controlli automatici.

Cerca: contenuto detto due volte (anche con parole diverse), frasi che partono a metà pensiero o finiscono prima del punto, pezzi rimasti che erano tentativi abbandonati, e pezzi tolti che invece servivano perché senza di loro il discorso non si capisce. Una ripetizione voluta — un ritornello — resta: dillo.

Se il montaggio è pulito rispondi ok. Altrimenti elenca le correzioni con i numeri delle parole originali: cut per togliere, keep per rimettere."""

DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first": {"type": "integer"},
                    "last": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "quote": {"type": "string", "description": "prima parola … ultima parola"},
                    "reason": {"type": "string"},
                },
                "required": ["first", "last", "keep", "quote", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string", "description": "in due righe, cosa dice il video montato"},
    },
    "required": ["segments", "summary"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "revise"]},
        "revisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first": {"type": "integer"},
                    "last": {"type": "integer"},
                    "action": {"type": "string", "enum": ["cut", "keep"]},
                    "reason": {"type": "string"},
                },
                "required": ["first", "last", "action", "reason"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["verdict", "revisions", "notes"],
    "additionalProperties": False,
}


class Model(Protocol):
    """Anything that can answer a prompt with JSON matching a schema."""

    name: str

    def ask(self, system: str, user: str, schema: dict) -> dict: ...


class ClaudeModel:
    """The model behind the editor, through the official SDK.

    Thinking is left to the model — on Fable it is always on and on Opus 5 it
    is adaptive by default — and the depth is set with `effort`. The answer is
    constrained to the schema by the API, so what comes back is JSON or an
    error, never prose to parse.
    """

    def __init__(self, api_key: str, *, model: str = "claude-opus-5"):
        if not api_key:
            raise ConfigurationError("SKYGROUND_ADVISER_API_KEY non configurata")
        self.api_key = api_key
        self.model = model
        self.name = model
        self.served_by: str | None = None

    def ask(self, system: str, user: str, schema: dict) -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        request = dict(
            max_tokens=32000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
            # A safety classifier on the newest models can decline a request
            # that is nothing of the sort — it did, on the first real run: a
            # numbered transcript of somebody talking about beauty salons was
            # read as an attempt to duplicate model outputs. With server-side
            # fallbacks the API reroutes the same call to another model, and
            # `response.model` says which one answered.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        try:
            with client.beta.messages.stream(model=self.model, **request) as stream:
                response = stream.get_final_message()
        except anthropic.NotFoundError:
            # The configured model is not on this account. Say which one ran
            # instead of quietly answering as if it were the one asked for.
            with client.beta.messages.stream(model="claude-opus-5", **request) as stream:
                response = stream.get_final_message()
        self.served_by = response.model
        if response.stop_reason == "refusal":
            raise ConfigurationError(
                "il modello ha rifiutato di montare questo girato"
                + (f": {response.stop_details.explanation}" if response.stop_details else "")
            )
        if response.stop_reason == "max_tokens":
            raise ConfigurationError("la risposta del modello è stata troncata")
        text = next((block.text for block in response.content if block.type == "text"), "")
        return json.loads(text or "{}")


class OpenAIModel:
    """The same job through OpenAI's Responses API.

    The editor never cared which model answers — it asks one question, in one
    shape, and reads JSON back. So a second provider is this class and nothing
    else, and two models can edit the same footage to be compared on the
    result rather than on reputation.
    """

    def __init__(self, api_key: str, *, model: str = "gpt-6", client=None):
        if not api_key and client is None:
            raise ConfigurationError("SKYGROUND_OPENAI_API_KEY non configurata")
        self.api_key = api_key
        self.model = model
        self.name = model
        self.served_by: str | None = None
        self._client = client

    def _sdk(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as error:  # pragma: no cover - optional extra
            raise ConfigurationError("montatore OpenAI selezionato ma il pacchetto openai non è installato") from error
        self._client = OpenAI(api_key=self.api_key)
        return self._client

    @staticmethod
    def available(client) -> list[str]:
        """The GPT-family model ids this key can call, newest first."""
        try:
            ids = [m.id for m in client.models.list()]
        except Exception:
            return []
        return sorted((i for i in ids if i.startswith(("gpt", "o"))), reverse=True)[:20]

    def ask(self, system: str, user: str, schema: dict) -> dict:
        client = self._sdk()
        request = dict(
            model=self.model,
            instructions=system,
            input=user,
            text={"format": {"type": "json_schema", "name": "montaggio", "schema": schema, "strict": True}},
        )
        try:
            try:
                response = client.responses.create(reasoning={"effort": "high"}, **request)
            except Exception as error:
                # A model without a reasoning dial rejects the parameter; ask
                # again without it rather than guess which models have one.
                if "reasoning" not in str(error).lower() or "not_found" in str(error).lower():
                    raise
                response = client.responses.create(**request)
        except Exception as error:
            if "model_not_found" in str(error) or "does not exist" in str(error):
                # Say which models this key *can* use, instead of a bare 500:
                # the name of the newest model is exactly the thing nobody
                # remembers correctly.
                raise ConfigurationError(
                    f"il modello «{self.model}» non esiste per questa chiave OpenAI; "
                    f"disponibili: {', '.join(self.available(client)) or 'nessuno elencato'}"
                ) from error
            if error.__class__.__module__.startswith("openai"):
                raise ConfigurationError(f"OpenAI: {str(error)[:300]}") from error
            raise
        self.served_by = getattr(response, "model", None) or self.model
        incomplete = getattr(response, "incomplete_details", None)
        if incomplete is not None:
            raise ConfigurationError(f"la risposta del modello è incompleta: {getattr(incomplete, 'reason', incomplete)}")
        text = getattr(response, "output_text", "") or ""
        return json.loads(text or "{}")


# ------------------------------------------------------------ the decision


@dataclass
class Decision:
    first: int
    last: int
    keep: bool
    reason: str
    by: str = "modello"


@dataclass
class Edit:
    decisions: list[Decision]
    summary: str = ""
    repairs: list[str] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)

    def kept(self, total: int) -> list[bool]:
        flags = [False] * total
        for decision in self.decisions:
            if decision.keep:
                for index in range(decision.first, decision.last + 1):
                    flags[index] = True
        return flags


def _norm(text: str) -> str:
    return takes.normalize(re.sub(r"[^\w']+", " ", text)).strip()


def parse_edit(payload: dict, words: list[Word]) -> Edit:
    """Turn the model's answer into decisions that cover every word once.

    The model is asked for contiguous segments and usually returns them. When
    it does not — an overlap, a gap, a segment past the end — the answer is
    repaired rather than trusted or discarded: overlaps are trimmed, a gap is
    *kept* (the safe way to be wrong, since nothing is lost by it), and every
    repair is written down. The quote is checked against the words at the
    numbers given; a mismatch is a miscount, and the quote is looked for near
    the claimed place before the numbers are believed.
    """
    total = len(words)
    raw = sorted(
        (
            dict(item)
            for item in payload.get("segments", [])
            if isinstance(item, dict) and "first" in item and "last" in item
        ),
        key=lambda item: (int(item["first"]), int(item["last"])),
    )
    decisions: list[Decision] = []
    repairs: list[str] = []
    cursor = 0
    for item in raw:
        first, last = int(item["first"]), int(item["last"])
        first, last = _relocate(first, last, str(item.get("quote", "")), words, repairs)
        first, last = max(first, 0), min(last, total - 1)
        if last < first:
            continue
        if first > cursor:
            decisions.append(Decision(cursor, first - 1, True, "non classificato dal modello: tenuto"))
            repairs.append(f"parole {cursor}–{first - 1} non coperte, tenute")
        if first < cursor:
            repairs.append(f"segmento {first}–{last} si sovrappone, tagliato a {cursor}")
            first = cursor
            if last < first:
                continue
        decisions.append(
            Decision(first, last, bool(item.get("keep", True)), str(item.get("reason", ""))[:300])
        )
        cursor = last + 1
    if cursor < total:
        decisions.append(Decision(cursor, total - 1, True, "non classificato dal modello: tenuto"))
        repairs.append(f"parole {cursor}–{total - 1} non coperte, tenute")
    return Edit(decisions=_coalesce(decisions), summary=str(payload.get("summary", ""))[:500], repairs=repairs)


def _relocate(first: int, last: int, quote: str, words: list[Word], repairs: list[str]) -> tuple[int, int]:
    """Believe the numbers when the quote agrees; otherwise find the quote."""
    if not quote or not (0 <= first < len(words)):
        return first, last
    parts = [part for part in re.split(r"\s*(?:…|\.\.\.|→|-)\s*", quote) if part.strip()]
    head = _norm(parts[0]).split()[:2] if parts else []
    tail = _norm(parts[-1]).split()[-2:] if parts else []
    if not head:
        return first, last
    head_ok = _matches(words, first, head)
    tail_ok = not tail or _matches(words, last - len(tail) + 1, tail)
    if head_ok and tail_ok:
        return first, last
    # Each end is looked for on its own: a miscount at the start says nothing
    # about the end, and moving both by the same amount was its own mistake.
    if not head_ok:
        starts = [i for i in range(max(0, first - 12), min(len(words), first + 12)) if _matches(words, i, head)]
        if len(starts) == 1:
            repairs.append(f"inizio «{' '.join(head)}» era a {first}, trovato a {starts[0]}")
            first = starts[0]
    if tail and not tail_ok:
        ends = [i + len(tail) - 1 for i in range(max(0, last - 12), min(len(words), last + 12))
                if _matches(words, i, tail)]
        if len(ends) == 1:
            repairs.append(f"fine «{' '.join(tail)}» era a {last}, trovata a {ends[0]}")
            last = ends[0]
    return first, last


def _matches(words: list[Word], at: int, tokens_: list[str]) -> bool:
    if at < 0 or at + len(tokens_) > len(words):
        return False
    return all(_norm(words[at + offset].s) == token for offset, token in enumerate(tokens_))


def _coalesce(decisions: list[Decision]) -> list[Decision]:
    """Adjacent decisions of the same kind are one decision."""
    out: list[Decision] = []
    for decision in decisions:
        if out and out[-1].keep == decision.keep and out[-1].last + 1 == decision.first and (
            decision.keep or out[-1].reason == decision.reason
        ):
            out[-1] = Decision(out[-1].first, decision.last, decision.keep,
                               out[-1].reason if out[-1].reason else decision.reason, out[-1].by)
        else:
            out.append(decision)
    return out


def apply_revisions(edit: Edit, revisions: list[dict], total: int, *, by: str) -> Edit:
    """Overlay cut/keep revisions on an edit, as new decisions on the record."""
    flags = edit.kept(total)
    reasons: dict[int, str] = {}
    for decision in edit.decisions:
        if not decision.keep:
            for index in range(decision.first, decision.last + 1):
                reasons[index] = decision.reason
    for item in revisions:
        try:
            first, last = max(0, int(item["first"])), min(total - 1, int(item["last"]))
            action = str(item["action"])
        except (KeyError, TypeError, ValueError):
            continue
        if last < first or action not in ("cut", "keep"):
            continue
        for index in range(first, last + 1):
            flags[index] = action == "keep"
            if action == "cut":
                reasons[index] = f"{by}: {str(item.get('reason', ''))[:200]}"
            else:
                reasons.pop(index, None)
    decisions: list[Decision] = []
    start = 0
    for index in range(1, total + 1):
        boundary = index == total or flags[index] != flags[start] or (
            not flags[start] and reasons.get(index) != reasons.get(start)
        )
        if boundary:
            keep = flags[start]
            decisions.append(Decision(start, index - 1, keep, "" if keep else reasons.get(start, "")))
            start = index
    return Edit(decisions, edit.summary, list(edit.repairs), list(edit.reviews))


# ----------------------------------------------------------------- the gates


def repeated_content(words: list[Word], kept: list[bool], n: int = REPEAT) -> list[str]:
    """Runs of `n` words that occur twice in what survived. Deterministic.

    Found by the code because the code cannot miss it; judged by the reviewer
    because a refrain is allowed to repeat and a number cannot tell.
    """
    stream = [(index, _norm(words[index].s)) for index in range(len(words)) if kept[index]]
    seen: dict[tuple[str, ...], int] = {}
    found: list[str] = []
    reported: set[int] = set()
    for position in range(len(stream) - n + 1):
        gram = tuple(token for _, token in stream[position : position + n])
        if not all(gram):
            continue
        if gram in seen:
            first_at = stream[seen[gram]][0]
            here = stream[position][0]
            if first_at in reported:
                continue
            reported.add(first_at)
            found.append(f"«{' '.join(gram)}» alle parole {first_at} e {here}")
        else:
            seen[gram] = position
    return found


def internal_holes(words: list[Word], kept: list[bool], segments, policy: CutPolicy = EDITOR_POLICY,
                   threshold: float = HOLE) -> list[str]:
    """Air the *result* would have between two words, when it is more than a breath.

    Measured on the output, not on the footage: a pause the policy has already
    split and padded is two tenths of a second in the film however long it was
    on set. The first version measured the footage and sent the reviewer
    twenty-eight holes that did not exist.
    """
    segment_of: dict[int, int] = {}
    for number, segment in enumerate(segments):
        if segment.first_word is None or segment.last_word is None:
            continue
        for index in range(segment.first_word, segment.last_word + 1):
            segment_of[index] = number
    found = []
    previous = None
    for index in range(len(words)):
        if not kept[index]:
            continue
        if previous is not None:
            if segment_of.get(index) == segment_of.get(previous):
                gap = words[index].t - words[previous].end
            else:
                gap = policy.lead_out + policy.lead_in
            if gap > threshold:
                found.append(f"{gap:.2f}s di vuoto fra «{words[previous].s}» ({previous}) e «{words[index].s}» ({index})")
        previous = index
    return found


def mid_thought_starts(words: list[Word], kept: list[bool]) -> list[str]:
    """A kept run that begins right after a kept-out word with no sentence end
    before it is a candidate for starting mid-thought. Only a candidate."""
    found = []
    for index in range(1, len(words)):
        if kept[index] and not kept[index - 1]:
            before = words[index - 1].s
            if not re.search(r"[.!?…]$", before):
                snippet = " ".join(w.s for w in words[index : index + 6])
                found.append(f"clip che comincia a «{snippet}» ({index}) dopo un taglio dentro la frase")
    return found


# ------------------------------------------------------------------ the plan


def plan_edit(
    analysis: Analysis,
    model: Model,
    *,
    decisions: dict[str, str] | None = None,
    policy: CutPolicy | None = None,
    now: str | None = None,
) -> CutPlan:
    """Read, decide, realise, review, realise again. Then the invariants."""
    from datetime import UTC, datetime

    policy = policy or EDITOR_POLICY
    decisions = dict(decisions or {})
    analysis = align.prepare(analysis)
    words = analysis.words
    utterances = takes.build_utterances(words, gap=policy.utterance_gap)

    plan = CutPlan(
        source=analysis.source,
        source_duration=analysis.duration,
        policy=policy.as_dict(),
        generated_at=now or datetime.now(UTC).isoformat(timespec="seconds"),
    )
    if not words:
        return plan
    plan.utterances = utterances

    edit = parse_edit(model.ask(DECIDE, present(words, utterances), DECIDE_SCHEMA), words)

    for _round in range(REVIEW_ROUNDS):
        kept = _with_people(edit, decisions, len(words)).kept(len(words))
        segments = _realise(analysis, utterances, kept, policy)
        findings = (
            [f"detto due volte: {line}" for line in repeated_content(words, kept)]
            + [f"buco: {line}" for line in internal_holes(words, kept, segments, policy)]
            + [f"attacco: {line}" for line in mid_thought_starts(words, kept)]
        )
        report = present_result(words, kept, segments)
        if findings:
            report += "\n\nSEGNALAZIONI AUTOMATICHE\n" + "\n".join(f"- {line}" for line in findings)
        answer = model.ask(REVIEW, report, REVIEW_SCHEMA)
        edit.reviews.append(f"giro {_round + 1}: {answer.get('verdict')} — {str(answer.get('notes', ''))[:300]}")
        if answer.get("verdict") != "revise" or not answer.get("revisions"):
            break
        edit = apply_revisions(edit, answer["revisions"], len(words), by="rilettura")

    final = _with_people(edit, decisions, len(words))
    kept = final.kept(len(words))
    plan.segments = _realise(analysis, utterances, kept, policy)
    for utterance in utterances:
        utterance.kept = any(kept[utterance.first_word : utterance.last_word + 1])
        if not utterance.kept:
            utterance.drop_reason = next(
                (d.reason for d in final.decisions if not d.keep and d.first <= utterance.first_word <= d.last),
                "tolto dal montatore",
            )
    plan.questions = _questions(final, words, decisions)
    dropped = {u.index: u.drop_reason for u in utterances if not u.kept}
    plan.removed = _classify_removed(analysis, plan.segments, utterances, dropped)
    plan.editor = {
        "model": getattr(model, "served_by", None) or model.name,
        "summary": edit.summary,
        "repairs": edit.repairs,
        "reviews": edit.reviews,
        # The model's edit itself, so a person's answer can be laid over it
        # later without asking the model again — twice, and differently.
        "decisions": [
            {"first": d.first, "last": d.last, "keep": d.keep, "reason": d.reason}
            for d in edit.decisions
        ],
    }
    return plan


def replan(analysis: Analysis, previous: CutPlan, decisions: dict[str, str]) -> CutPlan:
    """The same edit with a person's answers laid over it. No model call.

    Answering a question is not a reason to ask the model to edit the film
    again: it would take a minute, cost money, and come back different. The
    model's decisions are on the plan; this reads them back and re-realises.
    """
    from datetime import UTC, datetime

    policy = CutPolicy(**{k: v for k, v in (previous.policy or {}).items() if k in CutPolicy().as_dict()}) \
        if previous.policy else EDITOR_POLICY
    analysis = align.prepare(analysis)
    words = analysis.words
    utterances = takes.build_utterances(words, gap=policy.utterance_gap)
    stored = previous.editor.get("decisions") or []
    edit = Edit(
        decisions=[Decision(int(d["first"]), int(d["last"]), bool(d["keep"]), str(d.get("reason", "")))
                   for d in stored],
        summary=str(previous.editor.get("summary", "")),
        repairs=list(previous.editor.get("repairs", [])),
        reviews=list(previous.editor.get("reviews", [])),
    )
    plan = CutPlan(
        source=analysis.source,
        source_duration=analysis.duration,
        policy=policy.as_dict(),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    plan.utterances = utterances
    final = _with_people(edit, decisions, len(words))
    kept = final.kept(len(words))
    plan.segments = _realise(analysis, utterances, kept, policy)
    for utterance in utterances:
        utterance.kept = any(kept[utterance.first_word : utterance.last_word + 1])
        if not utterance.kept:
            utterance.drop_reason = next(
                (d.reason for d in final.decisions if not d.keep and d.first <= utterance.first_word <= d.last),
                "tolto dal montatore",
            )
    plan.questions = _questions(final, words, decisions)
    dropped = {u.index: u.drop_reason for u in utterances if not u.kept}
    plan.removed = _classify_removed(analysis, plan.segments, utterances, dropped)
    plan.editor = dict(previous.editor)
    return plan


def policy_from(stored: dict | None) -> CutPolicy:
    """The editor's timing policy, with a project's overrides on top."""
    base = EDITOR_POLICY.as_dict()
    for key, value in (stored or {}).items():
        if key in base:
            base[key] = value
    return CutPolicy(**base)


def _with_people(edit: Edit, decisions: dict[str, str], total: int) -> Edit:
    """A person's answers override the model's, and stay on the record."""
    revisions = []
    for question_id, answer in decisions.items():
        if not question_id.startswith("edit:"):
            continue
        try:
            first, last = (int(part) for part in question_id[5:].split("-"))
        except ValueError:
            continue
        revisions.append({"first": first, "last": last, "action": answer, "reason": "scelta a mano"})
    return apply_revisions(edit, revisions, total, by="editor") if revisions else edit


def _realise(analysis: Analysis, utterances, kept: list[bool], policy: CutPolicy):
    scratch = CutPlan(source=analysis.source, source_duration=analysis.duration)
    return _build_segments(analysis, utterances, kept, policy, scratch, {})


def _questions(edit: Edit, words: list[Word], decisions: dict[str, str]) -> list[Question]:
    """Every cut the model made, as a decision a person can hear and reverse."""
    questions: list[Question] = []
    for decision in edit.decisions:
        if decision.keep:
            continue
        start, end = words[decision.first].t, words[decision.last].end
        said = " ".join(word.s for word in words[decision.first : decision.last + 1])
        question_id = f"edit:{decision.first}-{decision.last}"
        answered = decisions.get(question_id)
        question = Question(
            id=question_id,
            kind=ASK_EDIT,
            at=start,
            prompt="Questo pezzo va tolto?",
            context=f"{decision.reason or 'tolto dal montatore'}. Il pezzo: «{said[:140]}»",
            options=[
                Option(id="cut", label="Toglierlo", detail=f"{end - start:.1f}s · {decision.reason[:60]}",
                       recommended=True, start=start, end=end),
                Option(id="keep", label="Tenerlo", detail="Resta nel montaggio.",
                       start=max(0.0, start - 1.0), end=end + 1.0),
            ],
            answer=answered or "cut",
            answered_by=None if answered else ENGINE,
        )
        questions.append(question)
    # Reinstated by hand: the record shows that too.
    for question_id, answer in decisions.items():
        if question_id.startswith("edit:") and answer == "keep" and not any(q.id == question_id for q in questions):
            try:
                first, last = (int(part) for part in question_id[5:].split("-"))
            except ValueError:
                continue
            if 0 <= first <= last < len(words):
                said = " ".join(word.s for word in words[first : last + 1])
                questions.append(Question(
                    id=question_id, kind=ASK_EDIT, at=words[first].t, prompt="Questo pezzo va tolto?",
                    context=f"Rimesso a mano. Il pezzo: «{said[:140]}»",
                    options=[
                        Option(id="cut", label="Toglierlo", start=words[first].t, end=words[last].end),
                        Option(id="keep", label="Tenerlo", detail="Resta nel montaggio.",
                               start=max(0.0, words[first].t - 1.0), end=words[last].end + 1.0),
                    ],
                    answer="keep",
                ))
    questions.sort(key=lambda question: question.at)
    return questions


def build_model(settings=None, override: dict | None = None) -> Model | None:
    """The configured model, or None when the studio has no key.

    `override` — `{"provider": ..., "model": ...}` from a request — picks a
    different editor for one run, which is how two models get compared on the
    same footage without flipping the studio's default.
    """
    from skyground.config import get_settings

    settings = settings or get_settings()
    override = override or {}
    provider = str(override.get("provider") or settings.editor_provider or "claude").strip().lower()
    if provider in ("none", "", "off"):
        return None
    if provider in ("claude", "anthropic"):
        if (settings.adviser_provider or "none").strip().lower() in ("none", "", "off") and not override:
            return None
        model = str(override.get("model") or (settings.editor_model if settings.editor_provider in ("claude", "anthropic") else "claude-opus-5"))
        return ClaudeModel(settings.adviser_api_key or "", model=model)
    if provider in ("openai", "gpt"):
        model = str(override.get("model") or (settings.editor_model if settings.editor_provider == "openai" else "gpt-6"))
        return OpenAIModel(settings.openai_api_key or "", model=model)
    raise ConfigurationError(f"montatore sconosciuto: {provider}")
