"""What the timeline needs to draw the footage: peaks, thumbnails, a manifest.

The worker runs ffmpeg; this module is the arithmetic around it, kept pure so
the numbers can be checked without a media file. One proxy job produces three
artefacts and an index that says where they are:

* `proxy/source.mp4` — the browser-playable copy, 30 fps, one keyframe every
  15 frames so a seek decodes at most half a second of video;
* `proxy/peaks.u8` — the loudness of the audio, one byte per 10 ms, which is
  what a waveform is at any zoom the timeline can show;
* `proxy/thumbs-NNN.jpg` — one frame a second, tiled into sheets, so scrubbing
  has a picture to show before the video element has decoded one;
* `proxy/index.json` — the manifest below.
"""

from __future__ import annotations

import math
from array import array
from typing import Any, BinaryIO

SCHEMA_VERSION = 1

#: The proxy's frame grid: the studio's own, so frame N here is frame N in the
#: render. And its keyframe interval, the price of a seek.
PROXY_FPS = 30
PROXY_GOP = 15

#: Peaks: mono audio at 8 kHz, the loudest sample of every 80 → 100 values per
#: second. Six minutes of footage are 36 KB.
PEAK_SAMPLE_RATE = 8000
PEAK_WINDOW = 80
PEAK_RATE = PEAK_SAMPLE_RATE // PEAK_WINDOW

#: Thumbnails: one per second, portrait, tiled 20 across and 30 down so a
#: sheet holds ten minutes and stays under any canvas texture limit.
THUMB_FPS = 1
THUMB_WIDTH = 68
THUMB_HEIGHT = 120
THUMB_COLUMNS = 20
THUMB_ROWS = 30
THUMBS_PER_SHEET = THUMB_COLUMNS * THUMB_ROWS


def peaks_from_pcm(stream: BinaryIO, *, window: int = PEAK_WINDOW) -> bytes:
    """One byte per window of signed 16-bit mono samples: the loudest sample's
    magnitude, scaled to 0–255. A trailing partial window counts as one."""
    peaks = bytearray()
    carry = array("h")
    while True:
        chunk = stream.read(window * 2 * 256)
        if not chunk:
            break
        if len(chunk) % 2:  # a torn sample at a chunk edge: wait for its other half
            chunk += stream.read(1)
        samples = array("h")
        samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
        if carry:
            carry.extend(samples)
            samples = carry
            carry = array("h")
        whole = (len(samples) // window) * window
        for start in range(0, whole, window):
            peaks.append(_peak(samples[start : start + window]))
        carry = samples[whole:]
    if carry:
        peaks.append(_peak(carry))
    return bytes(peaks)


def _peak(samples) -> int:
    loudest = 0
    for sample in samples:
        magnitude = -sample if sample < 0 else sample
        if magnitude > loudest:
            loudest = magnitude
    return min(255, (loudest * 255) // 32767)


def thumbnail_layout(duration: float) -> dict[str, int]:
    """How many thumbnails a source of `duration` seconds gets, and how many
    sheets they fill. Never zero: a one-frame film still has a picture."""
    count = max(1, math.ceil(max(0.0, duration) * THUMB_FPS))
    return {
        "count": count,
        "sheets": math.ceil(count / THUMBS_PER_SHEET),
        "columns": THUMB_COLUMNS,
        "rows": THUMB_ROWS,
        "perSheet": THUMBS_PER_SHEET,
        "width": THUMB_WIDTH,
        "height": THUMB_HEIGHT,
        "fps": THUMB_FPS,
    }


def thumbnail_filter() -> str:
    """The ffmpeg video filter that produces the sheets."""
    return (
        f"fps={THUMB_FPS},"
        f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT},"
        f"tile={THUMB_COLUMNS}x{THUMB_ROWS}"
    )


def manifest(
    *,
    proxy_key: str,
    codec: str,
    duration: float,
    peaks_key: str,
    thumb_keys: list[str],
    source_sha256: str = "",
) -> dict[str, Any]:
    """The index the editor reads first: every key, and the numbers it needs
    to place them in time."""
    layout = thumbnail_layout(duration)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "proxy": {"key": proxy_key, "codec": codec, "fps": PROXY_FPS, "gop": PROXY_GOP},
        "duration": round(duration, 3),
        "peaks": {"key": peaks_key, "rate": PEAK_RATE},
        "thumbs": {"keys": list(thumb_keys), **layout},
        "sourceSha256": source_sha256,
    }
