"""Reading the media: silence detection, transcription adapters, and a check
against the real footage when it happens to be available."""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from skyground.analysis import audio, invariants
from skyground.analysis.cut import plan_cut
from skyground.analysis.models import Analysis, Word
from skyground.analysis.transcription import (
    DeepgramTranscriber,
    FixtureTranscriber,
    build_transcriber,
    parse_deepgram,
)
from skyground.config import load_settings
from skyground.errors import ConfigurationError, StudioError

FFMPEG_REPORT = """
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 4.26 | silence_duration: 4.26
[silencedetect @ 0x1] silence_start: 9.8
[silencedetect @ 0x1] silence_end: 14.74 | silence_duration: 4.94
[silencedetect @ 0x1] silence_start: 350.1
"""


def test_silences_are_read_from_the_ffmpeg_report(monkeypatch):
    monkeypatch.setattr(audio, "_run", lambda command: FFMPEG_REPORT)
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    silences = audio.detect_silences("raw.wav", duration=360.83)

    assert [(round(s.start, 2), round(s.end, 2)) for s in silences] == [
        (0.0, 4.26), (9.8, 14.74), (350.1, 360.83)
    ]


def test_a_take_ending_in_silence_still_closes_its_last_region(monkeypatch):
    """FFmpeg omits the closing marker at end of file; the parser must not."""
    monkeypatch.setattr(audio, "_run", lambda command: "silence_start: 12.0\n")
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    assert audio.detect_silences("raw.wav", duration=20.0)[-1].end == 20.0


def test_duration_is_read_from_the_header(monkeypatch):
    monkeypatch.setattr(audio, "_run", lambda command: "  Duration: 00:06:00.83, start: 0.0")
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    assert audio.probe_duration("raw.mov") == pytest.approx(360.83)


def test_an_unreadable_file_is_reported(monkeypatch):
    monkeypatch.setattr(audio, "_run", lambda command: "non un media")
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    with pytest.raises(StudioError):
        audio.probe_duration("appunti.txt")


# ------------------------------------------------------------- transcription


def test_the_provider_response_becomes_words():
    payload = {
        "results": {"channels": [{"alternatives": [{"words": [
            {"start": 1.0, "end": 1.4, "word": "ciao", "punctuated_word": "Ciao,", "confidence": 0.98},
            {"start": 1.5, "end": 2.0, "word": "mondo", "confidence": 0.91},
        ]}]}]}
    }
    words = parse_deepgram(payload)
    assert [word.s for word in words] == ["Ciao,", "mondo"]
    assert words[0].p == pytest.approx(0.98)


def test_an_empty_result_is_not_an_error():
    assert parse_deepgram({"results": {"channels": [{"alternatives": []}]}}) == []


def test_an_unrecognised_response_is_reported():
    with pytest.raises(StudioError):
        parse_deepgram({"qualcosa": "altro"})


def test_a_provider_without_a_key_is_refused_at_construction():
    with pytest.raises(ConfigurationError):
        DeepgramTranscriber("")


def test_a_transcript_on_disk_can_be_replayed(tmp_path):
    fixture = tmp_path / "transcript.json"
    fixture.write_text(json.dumps({"words": [{"t": 1, "end": 2, "s": "ciao", "p": 1}]}))
    words = FixtureTranscriber(fixture).transcribe(tmp_path / "audio.wav")
    assert [word.s for word in words] == ["ciao"]


def test_the_backend_follows_the_configuration(tmp_path):
    fixture = tmp_path / "t.json"
    fixture.write_text("[]")
    assert build_transcriber(load_settings({})).name == "whisper-local"
    assert (
        build_transcriber(
            load_settings({"SKYGROUND_TRANSCRIPTION_PROVIDER": f"fixture:{fixture}"})
        ).name
        == "fixture"
    )
    with pytest.raises(ConfigurationError):
        build_transcriber(load_settings({"SKYGROUND_TRANSCRIPTION_PROVIDER": "inventato"}))


# --------------------------------------------------- against the real footage

#: Point this at a transcript of the reference take to run the check below.
REAL_TRANSCRIPT = os.environ.get("SKYGROUND_REFERENCE_TRANSCRIPT", "")
REFERENCE_DURATION = 360.83


@pytest.mark.skipif(not REAL_TRANSCRIPT, reason="trascrizione di riferimento non disponibile")
def test_the_engine_holds_on_the_real_take():
    """Six minutes of real speech: every invariant, and a plan that says
    something useful rather than everything or nothing."""
    payload = json.loads(pathlib.Path(REAL_TRANSCRIPT).read_text(encoding="utf-8"))
    analysis = Analysis(
        source="assets/raw.mov",
        duration=REFERENCE_DURATION,
        words=[Word.from_dict(word) for word in payload["words"]],
    )
    plan = plan_cut(analysis)

    assert invariants.check(plan, analysis) == []
    # It removed a real amount of dead air, and did not eat the video.
    assert 0.20 < (1 - plan.output_duration / analysis.duration) < 0.75
    # It asked about the repeated openings rather than choosing one.
    assert plan.open_questions
    assert plan.status == "draft"


# ----------------------------------------------------------------- the proxy


def test_the_proxy_defaults_to_the_codec_every_browser_decodes():
    """The camera original is HEVC in a .mov, which no browser plays. The
    preview only exists because the worker makes a copy that does."""
    from skyground.worker.runner import PROXY_FORMATS

    name, media_type, encoder = PROXY_FORMATS[load_settings({}).proxy_codec]
    assert (name, media_type) == ("source.mp4", "video/mp4")
    assert "libx264" in encoder
    assert "+faststart" in encoder  # the index up front, so scrubbing starts at once


def test_an_open_codec_is_available_for_builds_without_the_licensed_one():
    from skyground.worker.runner import PROXY_FORMATS

    name, media_type, encoder = PROXY_FORMATS[
        load_settings({"SKYGROUND_PROXY_CODEC": "vp9"}).proxy_codec
    ]
    assert (name, media_type) == ("source.webm", "video/webm")
    assert "libvpx-vp9" in encoder


def test_an_unknown_codec_falls_back_instead_of_failing_the_job():
    from skyground.worker.runner import PROXY_FORMATS

    assert PROXY_FORMATS.get(load_settings({"SKYGROUND_PROXY_CODEC": "boh"}).proxy_codec) is None
