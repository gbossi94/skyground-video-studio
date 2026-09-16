"""Moving the editorial layers when the cut underneath them moves.

`timeline.json` says which pieces of the source survive and in what order.
Everything laid on top of it — the word-by-word captions, the motion graphics —
is written in *output* time, so the moment the cut changes they all point at the
wrong frames. Re-rendering then produces a video where the subtitles run ahead
of the voice and a card lands on the wrong sentence.

Nothing here decides anything. It moves things that were already decided:

* a caption belongs to a word, and a word has a place in the source, so the
  caption goes wherever that word went;
* a card was placed against a moment of the film, so it follows that moment —
  and if the cut removed the moment, the card has nothing left to sit on and is
  taken out rather than left floating at the wrong time.

The functions are pure and take the clips as data, so they can be tested and
read without a project on disk.
"""

from __future__ import annotations

from typing import Any

#: Mezzo frame a 30 fps: sotto questa soglia due tempi sono lo stesso tempo.
TOLERANCE = 1.0 / 60.0


def snap_to_frames(clips: list[dict], fps: int = 30) -> float:
    """Place every clip on a whole frame, in place. Returns the total length.

    Film is made of frames: a clip cut at 4.575–9.494 cannot last 4.919 seconds,
    it lasts 148 frames. Rounding that at encode time and *then* writing the
    result back into the timeline is what made the layers on top wrong — the
    captions had already been placed against numbers the encoder was about to
    change, and nobody noticed because each step, on its own, added up.

    So the rounding happens once, here, and both the timeline and the encoder
    read the same numbers. Whether the captions are placed before or after the
    source is rebuilt stops mattering, which is the point: an order that has to
    be remembered is an order that will eventually be forgotten.
    """
    cursor = 0.0
    for clip in clips:
        frames = max(1, round((float(clip["end"]) - float(clip["start"])) * fps))
        clip["output_start"] = round(cursor, 6)
        clip["frames"] = frames
        cursor += frames / fps
    return round(cursor, 6)


def spans(clips: list[dict]) -> list[tuple[float, float, float, float]]:
    """(output start, output end, source start, source end) for each clip.

    `output_start` is read when it is there and recomputed when it is not, so a
    timeline written by hand works the same as one written by the engine.
    """
    placed: list[tuple[float, float, float, float]] = []
    cursor = 0.0
    for clip in clips:
        start, end = float(clip["start"]), float(clip["end"])
        at = float(clip.get("output_start", cursor))
        placed.append((at, at + (end - start), start, end))
        cursor = at + (end - start)
    return placed


def to_source(at: float, clips: list[dict]) -> float | None:
    """Where an output moment came from, or None if it is past the end."""
    for out_start, out_end, src_start, _ in spans(clips):
        if at < out_start - TOLERANCE:
            return None
        if at <= out_end + TOLERANCE:
            return src_start + (at - out_start)
    return None


def to_output(at: float, clips: list[dict]) -> float | None:
    """Where a source moment ended up, or None if the cut removed it."""
    for out_start, _, src_start, src_end in spans(clips):
        if src_start - TOLERANCE <= at <= src_end + TOLERANCE:
            return out_start + max(0.0, at - src_start)
    return None


def captions_from(words: list[Any], clips: list[dict]) -> list[dict]:
    """The words that survived the cut, timed where they now are.

    A word straddling a cut point is kept for the part that survived: the sound
    of it is in the film, so the subtitle has to be too.
    """
    written: list[dict] = []
    for out_start, out_end, src_start, src_end in spans(clips):
        for word in words:
            start, end = float(word.t), float(word.end)
            if end <= src_start + TOLERANCE or start >= src_end - TOLERANCE:
                continue
            visible_start = max(start, src_start)
            visible_end = min(end, src_end)
            if visible_end - visible_start <= 0:
                continue
            written.append(
                {
                    "t": round(out_start + (visible_start - src_start), 3),
                    "end": round(min(out_end, out_start + (visible_end - src_start)), 3),
                    "s": word.s,
                }
            )
    written.sort(key=lambda item: item["t"])
    return written


def move_cards(
    cards: list[dict], old_clips: list[dict], new_clips: list[dict]
) -> tuple[list[dict], list[str]]:
    """Carry each card from the old cut to the new one, or drop it.

    Returns the cards that still have somewhere to be, and a line for each one
    that no longer does — because a graphic disappearing from the film is not
    something to do silently.
    """
    moved: list[dict] = []
    lost: list[str] = []
    total = spans(new_clips)[-1][1] if new_clips else 0.0

    for card in cards:
        source_at = to_source(float(card["a"]), old_clips)
        if source_at is None:
            lost.append(f"{card['id']}: il momento a cui era agganciata non c'è più")
            continue
        at = to_output(source_at, new_clips)
        if at is None:
            lost.append(f"{card['id']}: «{card['label']}» cadeva su un pezzo tolto dal montaggio")
            continue
        length = float(card["b"]) - float(card["a"])
        landed = dict(card)
        landed["a"] = round(at, 3)
        landed["b"] = round(min(total, at + length), 3)
        if landed["b"] - landed["a"] < 0.4:
            lost.append(f"{card['id']}: resterebbe in video meno di mezzo secondo")
            continue
        moved.append(landed)

    moved.sort(key=lambda item: item["a"])
    return moved, lost


def move_angles(
    angles: list[dict], old_clips: list[dict], new_clips: list[dict]
) -> tuple[list[dict], list[str]]:
    """The same for the alternative-angle inserts.

    An insert carries `start` and `duration` in output time plus `mediaStart`,
    an offset inside its own file. Only the first moves: where the insert sits
    in the film changes, which frame of it plays first does not.
    """
    moved: list[dict] = []
    lost: list[str] = []
    total = spans(new_clips)[-1][1] if new_clips else 0.0

    for angle in angles:
        source_at = to_source(float(angle["start"]), old_clips)
        at = to_output(source_at, new_clips) if source_at is not None else None
        if at is None:
            lost.append(f"{angle.get('id', '?')}: cadeva su un pezzo tolto dal montaggio")
            continue
        landed = dict(angle)
        landed["start"] = round(at, 3)
        landed["duration"] = round(min(float(angle["duration"]), max(0.0, total - at)), 3)
        if landed["duration"] <= 0:
            lost.append(f"{angle.get('id', '?')}: cadrebbe oltre la fine del montaggio")
            continue
        moved.append(landed)

    moved.sort(key=lambda item: item["start"])
    return moved, lost
