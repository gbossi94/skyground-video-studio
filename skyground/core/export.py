"""The cut, handed to an editor a person already knows.

The studio does the bulk of the work — listening, deciding, cutting to the
frame — and the last few decisions are better taken by a hand on a timeline.
`timeline.json` already *is* a timeline: every clip names a stretch of the
raw footage and where it lands in the film. This writes it in the shapes the
common editors read.

FCPXML is the exchange format DaVinci Resolve, Premiere Pro and Final Cut Pro
all import: one sequence, the raw file as its only asset, one clip per clip of
the cut, at the frame. The captions travel beside it as SRT, in output time,
grouped the way the film shows them — SRT is what every editor, CapCut
included, imports as subtitles. Standard library only, like the rest of the
core.
"""

from __future__ import annotations

import pathlib
from xml.sax.saxutils import escape, quoteattr

from skyground.core import retime

#: The timescale FCPXML times are written in. Thirty-thousandths of a second
#: express both a 30 fps frame (1000/30000s) and a 29.97 one (1001/30000s)
#: exactly, so nothing is rounded twice on the way in.
SCALE = 30000


def _rational(seconds: float) -> str:
    return f"{round(seconds * SCALE)}/{SCALE}s"


def _frames(frames: int, fps: int) -> str:
    """A whole number of frames, as the editor wants it."""
    return f"{frames * (SCALE // fps)}/{SCALE}s"


def fcpxml(base: pathlib.Path, name: str, *, read_json) -> str:
    """The cut as an FCPXML 1.10 document.

    The asset points at the raw file by absolute path *and* by name: the path
    is right on the machine that wrote it, and every editor relinks by name
    when it is not — the raw sits next to the document in the download.
    """
    project = read_json(base / "project.json")
    timeline = read_json(base / project["files"]["timeline"])
    canvas = project["canvas"]
    fps = int(canvas.get("fps", 30))
    width, height = int(canvas["width"]), int(canvas["height"])
    clips = [dict(clip) for clip in timeline["clips"]]
    retime.snap_to_frames(clips, fps)

    raw = (base / timeline["source"]).resolve()
    source = project.get("source") or {}
    source_seconds = float(source.get("duration") or max(float(c["end"]) for c in clips))
    total_frames = sum(int(c["frames"]) for c in clips)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE fcpxml>",
        '<fcpxml version="1.10">',
        "  <resources>",
        f'    <format id="r1" name="FFVideoFormat{height}p{fps}" frameDuration="{SCALE // fps}/{SCALE}s" '
        f'width="{width}" height="{height}"/>',
        f'    <asset id="r2" name={quoteattr(raw.name)} start="0s" duration="{_rational(source_seconds)}" '
        f'hasVideo="1" hasAudio="1" format="r1" audioSources="1" audioChannels="2" audioRate="48000">',
        f'      <media-rep kind="original-media" src={quoteattr(raw.as_uri())}/>',
        "    </asset>",
        "  </resources>",
        "  <library>",
        '    <event name="Skyground">',
        f"      <project name={quoteattr(name)}>",
        f'        <sequence format="r1" duration="{_frames(total_frames, fps)}" tcStart="0s" tcFormat="NDF" '
        f'audioLayout="stereo" audioRate="48k">',
        "          <spine>",
    ]
    offset = 0
    for index, clip in enumerate(clips):
        frames = int(clip["frames"])
        label = str(clip.get("label") or f"clip {index + 1}")[:60]
        lines.append(
            f'            <asset-clip ref="r2" name={quoteattr(label)} offset="{_frames(offset, fps)}" '
            f'start="{_rational(float(clip["start"]))}" duration="{_frames(frames, fps)}" '
            f'format="r1" tcFormat="NDF" audioRole="dialogue"/>'
        )
        offset += frames
    lines += [
        "          </spine>",
        "        </sequence>",
        "      </project>",
        "    </event>",
        "  </library>",
        "</fcpxml>",
        "",
    ]
    return "\n".join(lines)


def _srt_time(seconds: float) -> str:
    total = max(0, round(seconds * 1000))
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def srt(base: pathlib.Path, *, read_json, caption_groups) -> str:
    """The captions as SubRip, in output time, one cue per on-screen line."""
    project = read_json(base / "project.json")
    files = project["files"]
    words = read_json(base / files["captions"])
    cards = read_json(base / files["cards"]) if (base / files["cards"]).exists() else []
    duration = float(project["canvas"]["duration"])
    cues = []
    for number, group in enumerate(caption_groups(words, cards, duration), start=1):
        text = " ".join(str(word["s"]) for word in group).strip()
        if not text:
            continue
        start, end = float(group[0]["t"]), float(group[-1]["end"])
        cues.append(f"{number}\n{_srt_time(start)} --> {_srt_time(end)}\n{escape(text)}\n")
    return "\n".join(cues) + ("\n" if cues else "")
