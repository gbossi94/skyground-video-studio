"""Che il suono e l'immagine restino insieme.

Il difetto che questi test esistono per non far tornare: il montaggio veniva
tagliato pezzo per pezzo, e ogni pezzo usciva col video arrotondato al
fotogramma intero e l'audio no — una quarantina di millisecondi a clip. `concat`
incolla i due flussi separatamente, quindi quello scarto si somma: dopo tre clip
la voce era avanti alle labbra, dopo trenta di più di un secondo.

La validazione di allora non lo prendeva perché controllava che la *durata
totale* tornasse. E tornava: la durata totale è quella del video. Un numero che
torna non è un film che funziona.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from skyground.core import retime, workspace

FFMPEG = shutil.which("ffmpeg") or os.environ.get("FFMPEG_BIN", "")
FFPROBE = shutil.which("ffprobe") or ""


# --------------------------------------------------- i conti, senza ffmpeg


def test_every_clip_lands_on_a_whole_frame():
    clips = [{"start": 4.575, "end": 9.494}, {"start": 20.92, "end": 24.407}]

    total = retime.snap_to_frames(clips, 30)

    # 4.919s non esiste in un film a 30 fotogrammi: esistono 148 fotogrammi.
    assert clips[0]["frames"] == 148
    assert clips[1]["frames"] == 105
    assert clips[1]["output_start"] == pytest.approx(148 / 30)
    assert total == pytest.approx((148 + 105) / 30)


def test_a_clip_shorter_than_a_frame_still_gets_one():
    clips = [{"start": 1.0, "end": 1.004}]
    retime.snap_to_frames(clips, 30)
    assert clips[0]["frames"] == 1


def test_snapping_twice_changes_nothing():
    """`build_source` lo rifà dopo che i sottotitoli sono già stati piazzati.
    Se i due conti non danno lo stesso risultato, i sottotitoli si spostano."""
    clips = [{"start": 4.575, "end": 9.494}, {"start": 20.92, "end": 24.407}]

    first = retime.snap_to_frames(clips, 30)
    starts = [clip["output_start"] for clip in clips]
    second = retime.snap_to_frames(clips, 30)

    assert second == first
    assert [clip["output_start"] for clip in clips] == starts


def test_the_captions_follow_the_snapped_clips():
    from skyground.analysis.models import Word

    clips = [{"start": 10.0, "end": 14.004}, {"start": 24.0, "end": 30.0}]
    retime.snap_to_frames(clips, 30)
    words = [Word(t=25.0, end=25.5, s="dopo", p=0.9)]

    captions = retime.captions_from(words, clips)

    # Il secondo clip parte dopo 120 fotogrammi, non dopo 4.004 secondi.
    assert captions[0]["t"] == pytest.approx(120 / 30 + 1.0, abs=0.001)


# ------------------------------------------------------ il file, con ffmpeg


needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="serve ffmpeg per guardare un file vero"
)


def make_clip(path: pathlib.Path, seconds: float) -> None:
    """Un pezzo di girato sintetico, con immagine e suono."""
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


@needs_ffmpeg
def test_stream_durations_reads_the_two_tracks_apart(tmp_path):
    clip = tmp_path / "prova.mp4"
    make_clip(clip, 2.0)

    video, audio = workspace.stream_durations(clip)

    assert video == pytest.approx(2.0, abs=0.05)
    assert audio == pytest.approx(2.0, abs=0.05)


@needs_ffmpeg
def test_a_piece_whose_sound_and_picture_differ_is_refused(tmp_path):
    """Il controllo che mancava. Un pezzo così, concatenato, sposta tutto
    quello che viene dopo."""
    storto = tmp_path / "storto.mov"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=2.0",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "pcm_s16le", str(storto)],
        check=True,
    )

    from skyground.errors import ValidationError

    with pytest.raises(ValidationError, match="labiale"):
        workspace._refuse_drift(storto, 30)


@needs_ffmpeg
def test_a_piece_cut_the_way_the_studio_cuts_it_is_accepted(tmp_path):
    lungo = tmp_path / "girato.mp4"
    make_clip(lungo, 6.0)
    pezzo = tmp_path / "pezzo.mov"
    frames = 148
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "0.575", "-i", str(lungo),
         "-vf", "fps=30", "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-video_track_timescale", "30000",
         "-af", "aresample=48000,apad", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
         "-t", f"{frames / 30:.6f}", str(pezzo)],
        check=True,
    )

    video, audio = workspace.stream_durations(pezzo)

    assert video == pytest.approx(frames / 30, abs=1 / 600)
    assert audio == pytest.approx(frames / 30, abs=1 / 600)
    workspace._refuse_drift(pezzo, 30)  # non solleva


@needs_ffmpeg
def test_concatenating_the_pieces_does_not_accumulate_drift(tmp_path):
    """Il difetto per intero, riprodotto e poi verificato assente.

    Con il vecchio taglio ogni pezzo portava una quarantina di millisecondi di
    scarto; qui se ne incollano otto e si guarda quanto è rimasto in fondo.
    """
    lungo = tmp_path / "girato.mp4"
    make_clip(lungo, 30.0)

    pieces = []
    total = 0
    for index, (start, frames) in enumerate(
        [(0.575, 148), (3.92, 105), (7.455, 134), (11.584, 129),
         (15.047, 102), (18.3, 97), (21.11, 118), (24.7, 91)]
    ):
        piece = tmp_path / f"{index:02d}.mov"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{start}", "-i", str(lungo),
             "-vf", "fps=30", "-frames:v", str(frames),
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-video_track_timescale", "30000",
             "-af", "aresample=48000,apad", "-c:a", "pcm_s16le",
             "-ar", "48000", "-ac", "2", "-t", f"{frames / 30:.6f}", str(piece)],
            check=True,
        )
        pieces.append(piece)
        total += frames

    listing = tmp_path / "pezzi.txt"
    listing.write_text("".join(f"file '{piece}'\n" for piece in pieces), encoding="utf-8")
    montato = tmp_path / "montato.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
         str(montato)],
        check=True,
    )

    video, audio = workspace.stream_durations(montato)

    assert video == pytest.approx(total / 30, abs=1 / 30)
    # Mezzo fotogramma su otto clip. Prima erano trecento millisecondi.
    assert abs(video - audio) < 1 / 60
    workspace._refuse_drift(montato, 30, allowance=1.0)
