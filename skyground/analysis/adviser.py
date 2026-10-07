"""The adviser: a model reading the transcript for meaning.

Word comparison finds an attempt that *restarts* a sentence. It cannot find an
attempt that says the same thing in different words — and the reference footage
contains exactly that: a passage the editor dropped whose replacement shares
almost no vocabulary with it.

So a language model gets one narrow job: read the utterances and point at pairs
that make the same point twice. It never removes anything. Its output becomes
questions, which a person answers, and the deterministic engine still owns every
invariant. An adviser that is absent, misconfigured or wrong costs the editor a
few extra seconds of video, never a broken cut.
"""

from __future__ import annotations

import json
from typing import Protocol

from skyground.analysis.models import Utterance
from skyground.errors import ConfigurationError

#: Asking about the whole transcript at once keeps the comparison global, which
#: is the point; a 10 minute take is still only a few thousand tokens.
SYSTEM = """Sei l'assistente di montaggio di Skyground.

Ricevi le battute trascritte di un girato grezzo, in ordine, con i tempi. Il
parlante sbaglia, si corregge e riformula: il tuo compito è indicare le coppie
in cui una battuta successiva **rifà** una battuta precedente, cioè dice la
stessa cosa con parole diverse.

Regole:
- Segnala solo ciò che è davvero una ripetizione del medesimo contenuto.
- Due battute che sviluppano un discorso, o che elencano cose diverse, NON sono
  una ripetizione: non segnalarle.
- Non decidere quale tenere e non proporre tagli: la scelta è di una persona.
- Se non trovi nulla, restituisci una lista vuota."""

SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "indice della battuta precedente"},
                    "b": {"type": "integer", "description": "indice della battuta che la rifà"},
                    "why": {"type": "string", "description": "in una frase, cosa hanno in comune"},
                },
                "required": ["a", "b", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pairs"],
    "additionalProperties": False,
}


class Adviser(Protocol):
    name: str

    def suspects(self, utterances: list[Utterance]) -> list[dict]: ...


class NullAdviser:
    """The default. The pipeline works without any model at all."""

    name = "none"

    def suspects(self, utterances: list[Utterance]) -> list[dict]:
        return []


class ClaudeAdviser:
    name = "claude"

    def __init__(self, api_key: str, *, model: str = "claude-opus-5", max_utterances: int = 400):
        if not api_key:
            raise ConfigurationError("SKYGROUND_ADVISER_API_KEY non configurata")
        self.api_key = api_key
        self.model = model
        self.max_utterances = max_utterances

    def suspects(self, utterances: list[Utterance]) -> list[dict]:
        if len(utterances) < 2:
            return []
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - optional extra
            raise ConfigurationError(
                "adviser Claude selezionato ma il pacchetto anthropic non è installato"
            ) from error

        client = anthropic.Anthropic(api_key=self.api_key)
        listing = "\n".join(
            f"[{utterance.index}] {utterance.start:.1f}s–{utterance.end:.1f}s  {utterance.text}"
            for utterance in utterances[: self.max_utterances]
        )
        response = client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": listing}],
        )
        if response.stop_reason == "refusal":
            return []
        text = next((block.text for block in response.content if block.type == "text"), "")
        return self._clean(text, len(utterances))

    def _clean(self, text: str, total: int) -> list[dict]:
        """Never trust the adviser's indices: it is data from a model, not truth."""
        try:
            payload = json.loads(text or "{}")
        except json.JSONDecodeError:
            return []
        cleaned = []
        for pair in payload.get("pairs", []):
            try:
                first, second = int(pair["a"]), int(pair["b"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= first < total and 0 <= second < total) or first == second:
                continue
            cleaned.append(
                {
                    "a": min(first, second),
                    "b": max(first, second),
                    "why": str(pair.get("why", ""))[:300],
                }
            )
        return cleaned


def build_adviser(settings=None) -> Adviser:
    from skyground.config import get_settings

    settings = settings or get_settings()
    provider = (settings.adviser_provider or "none").strip().lower()
    if provider in ("none", "", "off"):
        return NullAdviser()
    if provider in ("claude", "anthropic"):
        return ClaudeAdviser(settings.adviser_api_key or "", model=settings.adviser_model)
    raise ConfigurationError(f"adviser sconosciuto: {provider}")
