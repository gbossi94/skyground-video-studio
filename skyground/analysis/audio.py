"""Reading the audio: duration and where the speaker is not speaking.

Standard library plus FFmpeg, which the project already requires. Silence is
detected on the audio alone and is never used to decide *what* to cut — only to
know where a cut can land without touching speech.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

from skyground.analysis.models import Silence
from skyground.core.workspace import find_ffmpeg
from skyground.errors import StudioError

DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d\.\d+)")
SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")

#: Below this level, for at least this long, counts as a pause.
DEFAULT_NOISE_DB = -32.0
DEFAULT_MIN_SILENCE = 0.25


def probe_duration(path: pathlib.Path | str) -> float:
    """Length of a media file in seconds."""
    result = _run([find_ffmpeg(), "-hide_banner", "-i", str(path)])
    match = DURATION_PATTERN.search(result)
    if not match:
        raise StudioError(f"durata non leggibile: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def extract_audio(
    source: pathlib.Path | str, destination: pathlib.Path | str, *, rate: int = 16000
) -> pathlib.Path:
    """Write a mono WAV next to the source: what transcription and analysis read."""
    destination = pathlib.Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-vn", "-ac", "1", "-ar", str(rate),
            "-c:a", "pcm_s16le", str(destination),
        ],
        check=True,
    )
    return destination


def detect_silences(
    path: pathlib.Path | str,
    *,
    noise_db: float = DEFAULT_NOISE_DB,
    min_duration: float = DEFAULT_MIN_SILENCE,
    duration: float | None = None,
) -> list[Silence]:
    """Every stretch of the file quiet enough, and long enough, to cut in."""
    output = _run(
        [
            find_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f", "null", "-",
        ]
    )
    silences: list[Silence] = []
    start: float | None = None
    for line in output.splitlines():
        opening = SILENCE_START.search(line)
        if opening:
            start = max(0.0, float(opening.group(1)))
            continue
        closing = SILENCE_END.search(line)
        if closing and start is not None:
            silences.append(Silence(start=start, end=float(closing.group(1))))
            start = None
    if start is not None:
        # FFmpeg omits the closing marker when the file ends in silence.
        silences.append(Silence(start=start, end=duration if duration else start))
    return silences


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    # FFmpeg writes its report to stderr even on success.
    return (result.stderr or "") + (result.stdout or "")
