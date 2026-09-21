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
    """Whisper in the worker. Slower than an API, but needs no credential.

    It runs in a child process. The model and its arenas are a gigabyte while
    a six minute take is being heard, and a long-lived Python process hands
    almost none of that back to the system: on a two gigabyte instance the
    next job — or the web service beside it — met the ceiling and the whole
    container was restarted. A child process gives every byte back when it
    exits, and the worker that waits for it stays at a hundred megabytes.
    """

    name = "whisper-local"

    def __init__(
        self,
        model: str = "small",
        compute_type: str = "int8",
        cache_root: pathlib.Path | str | None = None,
        *,
        beam_size: int = 1,
        cpu_threads: int = 2,
        in_process: bool = False,
    ):
        self.model_name = model
        self.compute_type = compute_type
        #: Greedy by default: a beam of five holds five hypotheses per frame
        #: and buys, on speech this clean, punctuation rather than words.
        self.beam_size = beam_size
        #: Each thread keeps its own working buffers; two is the knee of the
        #: curve on a shared two-core instance.
        self.cpu_threads = cpu_threads
        #: Tests and the CLI can keep it here, where a traceback is readable.
        self.in_process = in_process
        #: Where the weights are kept. On a deployment this belongs on the
        #: mounted disk: the container's own filesystem is thrown away at every
        #: deploy, and half a gigabyte of model with it.
        self.cache_root = pathlib.Path(cache_root) if cache_root else None
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
            if self.cache_root is not None:
                self.cache_root.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                download_root=str(self.cache_root) if self.cache_root else None,
            )
        return self._model

    def transcribe(self, audio: pathlib.Path, *, language: str = "it") -> list[Word]:
        if self.in_process:
            return self._transcribe_here(audio, language)
        return self._transcribe_apart(audio, language)

    def _transcribe_here(self, audio: pathlib.Path, language: str) -> list[Word]:
        segments, _info = self._load().transcribe(
            str(audio), language=language, word_timestamps=True,
            vad_filter=False, beam_size=self.beam_size,
        )
        words: list[Word] = []
        for segment in segments:
            for word in segment.words or []:
                text = word.word.strip()
                if text:
                    words.append(Word(t=word.start, end=word.end, s=text, p=word.probability))
        return words

    def _transcribe_apart(self, audio: pathlib.Path, language: str) -> list[Word]:
        """The same, in a child process that exits when the words are out."""
        import subprocess
        import sys

        payload = json.dumps({
            "audio": str(audio), "language": language, "model": self.model_name,
            "compute_type": self.compute_type, "beam_size": self.beam_size,
            "cpu_threads": self.cpu_threads,
            "cache_root": str(self.cache_root) if self.cache_root else "",
        })
        result = subprocess.run(
            [sys.executable, "-m", "skyground.analysis.transcribe_once"],
            input=payload, capture_output=True, text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise StudioError(
                "la trascrizione non è riuscita: " + (detail[-1] if detail else f"codice {result.returncode}")
            )
        return [Word.from_dict(word) for word in json.loads(result.stdout)["words"]]


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
        return LocalWhisperTranscriber(
            model=settings.transcription_model or "small",
            cache_root=settings.model_cache_root or None,
            beam_size=getattr(settings, "transcription_beam_size", 1),
            cpu_threads=getattr(settings, "transcription_threads", 2),
        )
    if provider.startswith("fixture:"):
        return FixtureTranscriber(provider.split(":", 1)[1])
    raise ConfigurationError(f"provider di trascrizione sconosciuto: {provider}")
