"""Turning speech into words with timings.

An adapter, because the choice of provider is a deployment decision and the cut
engine must not care: it needs words with start, end and a confidence, and
nothing else. Three backends ship:

* `deepgram` — the production path, word-level timings over HTTPS;
* `local` — Whisper running in the worker, no key and no per-minute cost,
  which is also what makes the pipeline testable offline;
* `fixture` — a transcript already on disk, used by tests and to re-run the
  engine on stored analysis without paying for transcription twice.
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from skyground.analysis.models import Word
from skyground.errors import ConfigurationError, StudioError

DEEPGRAM_ENDPOINT = "https://api.deepgram.com/v1/listen"


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: pathlib.Path, *, language: str = "it") -> list[Word]: ...


class FixtureTranscriber:
    """Reads a transcript that already exists. No network, no model."""

    name = "fixture"

    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)

    def transcribe(self, audio: pathlib.Path, *, language: str = "it") -> list[Word]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        words = payload["words"] if isinstance(payload, dict) else payload
        return [Word.from_dict(word) for word in words]


class LocalWhisperTranscriber:
    """Whisper in the worker. Slower than an API, but needs no credential."""

    name = "whisper-local"

    def __init__(self, model: str = "small", compute_type: str = "int8"):
        self.model_name = model
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:  # pragma: no cover - optional extra
                raise ConfigurationError(
                    "trascrizione locale non disponibile: installa faster-whisper "
                    "oppure configura un provider via API"
                ) from error
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type=self.compute_type
            )
        return self._model

    def transcribe(self, audio: pathlib.Path, *, language: str = "it") -> list[Word]:
        segments, _info = self._load().transcribe(
            str(audio), language=language, word_timestamps=True, vad_filter=False, beam_size=5
        )
        words: list[Word] = []
        for segment in segments:
            for word in segment.words or []:
                text = word.word.strip()
                if text:
                    words.append(Word(t=word.start, end=word.end, s=text, p=word.probability))
        return words


class DeepgramTranscriber:
    """Word level timings over HTTPS. The production default."""

    name = "deepgram"

    def __init__(self, api_key: str, *, model: str = "nova-2", timeout: int = 600):
        if not api_key:
            raise ConfigurationError("SKYGROUND_TRANSCRIPTION_API_KEY non configurata")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def transcribe(self, audio: pathlib.Path, *, language: str = "it") -> list[Word]:
        query = urllib.parse.urlencode(
            {"model": self.model, "language": language, "punctuate": "true", "smart_format": "true"}
        )
        request = urllib.request.Request(
            f"{DEEPGRAM_ENDPOINT}?{query}",
            data=pathlib.Path(audio).read_bytes(),
            method="POST",
            headers={"Authorization": f"Token {self.api_key}", "Content-Type": "audio/wav"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise StudioError(
                f"trascrizione rifiutata dal provider: {error.code} {error.reason}"
            ) from error
        except urllib.error.URLError as error:
            raise StudioError(
                f"provider di trascrizione irraggiungibile: {error.reason}"
            ) from error
        return parse_deepgram(payload)


def parse_deepgram(payload: dict) -> list[Word]:
    """Pull the word list out of a Deepgram response, whatever else it carries."""
    try:
        alternatives = payload["results"]["channels"][0]["alternatives"]
    except (KeyError, IndexError, TypeError) as error:
        raise StudioError("risposta di trascrizione non riconosciuta") from error
    if not alternatives:
        return []
    words = []
    for word in alternatives[0].get("words", []):
        text = str(word.get("punctuated_word") or word.get("word") or "").strip()
        if not text:
            continue
        words.append(
            Word(
                t=float(word["start"]),
                end=float(word["end"]),
                s=text,
                p=float(word.get("confidence", 1.0)),
            )
        )
    return words


def build_transcriber(settings=None) -> Transcriber:
    """Pick the backend the environment asks for."""
    from skyground.config import get_settings

    settings = settings or get_settings()
    provider = (settings.transcription_provider or "local").strip().lower()
    if provider == "deepgram":
        return DeepgramTranscriber(
            settings.transcription_api_key or "", model=settings.transcription_model
        )
    if provider == "local":
        return LocalWhisperTranscriber(model=settings.transcription_model or "small")
    if provider.startswith("fixture:"):
        return FixtureTranscriber(provider.split(":", 1)[1])
    raise ConfigurationError(f"provider di trascrizione sconosciuto: {provider}")
