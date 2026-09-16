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


# ------------------------------------------------- la voce, e da dove viene


@needs_ffmpeg
def test_the_soundtrack_is_rebuilt_from_the_cut_that_exists_now(tmp_path):
    """Il difetto che una persona ha davvero sentito.

    Lo studio sapeva rimontare l'immagine e non il suono: il renderizzatore
    legge `composition/soundtrack.m4a`, e quel file era il mix del montaggio
    *approvato* — un'altra disposizione dello stesso girato. Sopra un montaggio
    più corto andava d'accordo per una frase, quella d'apertura che i due
    montaggi hanno in comune, e da lì in poi la voce parlava d'altro rispetto
    alla bocca.
    """
    from skyground.core.workspace import Workspace

    root = tmp_path / "checkout"
    composition = root / "projects" / "prova" / "composition"
    composition.mkdir(parents=True)
    base = composition.parent

    # L'immagine rimontata: sei secondi.
    make_clip(composition / "source.mp4", 6.0)
    # E la colonna sonora di un montaggio precedente, lunga il doppio.
    make_clip(composition / "soundtrack.m4a", 12.0)
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
         "-c:a", "libmp3lame", str(composition / "music.mp3")],
        check=True,
    )
    (base / "project.json").write_text(
        '{"schemaVersion": 1, "canvas": {"width": 1080, "height": 1920, '
        '"fps": 30, "duration": 6.0}}',
        encoding="utf-8",
    )
    (base / "audio.json").write_text(
        '{"master": {"targetLufs": -16, "truePeak": -1.8},'
        ' "voice": {"asset": "composition/voice.m4a", "targetLufs": -16},'
        ' "music": {"asset": "composition/music.mp3", "targetLufs": -32, "loop": true},'
        ' "renderedMix": "composition/soundtrack.m4a",'
        ' "ducking": {"threshold": 0.03, "ratio": 4, "attackMs": 15, "releaseMs": 280}}',
        encoding="utf-8",
    )

    Workspace(root).build_soundtrack("prova")

    picture = workspace.measured_duration(composition / "source.mp4")
    for made in ("voice.m4a", "soundtrack.m4a"):
        assert abs(workspace.measured_duration(composition / made) - picture) < 1 / 30, (
            f"{made} non dura quanto l'immagine"
        )


@needs_ffmpeg
def test_a_mix_that_does_not_match_the_picture_is_refused(tmp_path):
    """Se il mix esce della lunghezza sbagliata lo studio deve fermarsi, non
    consegnarlo: sopra il montaggio non starebbe a tempo e nessun numero del
    render lo direbbe."""
    from skyground.core.workspace import Workspace
    from skyground.errors import ValidationError

    root = tmp_path / "checkout"
    composition = root / "projects" / "prova" / "composition"
    composition.mkdir(parents=True)
    base = composition.parent
    make_clip(composition / "source.mp4", 6.0)
    (base / "project.json").write_text(
        '{"schemaVersion": 1, "canvas": {"fps": 30, "duration": 6.0}}', encoding="utf-8"
    )
    # Nessuna musica e una voce che il mix copierà: qui si controlla il rifiuto,
    # quindi si punta il mix su un file già scritto della lunghezza sbagliata.
    (base / "audio.json").write_text(
        '{"voice": {"asset": "composition/voice.m4a", "targetLufs": -16},'
        ' "music": {"asset": "composition/assente.mp3"},'
        ' "renderedMix": "composition/soundtrack.m4a"}',
        encoding="utf-8",
    )

    workspace_under_test = Workspace(root)
    original = workspace.measured_duration

    def lying(path):
        # Il mix esce lungo il doppio: è la forma del difetto vero.
        return original(path) * 2 if path.name == "soundtrack.m4a" else original(path)

    workspace.measured_duration = lying
    try:
        with pytest.raises(ValidationError, match="a tempo"):
            workspace_under_test.build_soundtrack("prova")
    finally:
        workspace.measured_duration = original


@needs_ffmpeg
def test_a_29_97_fps_source_still_yields_whole_frames(tmp_path):
    """The production footage is 29.97 fps. After a seek its first frame is
    not at zero, and `fps=30` counting from there produced 137 frames where
    138 were asked for — 33 ms of drift, caught by the guard on the first
    unattended run. The timestamps are reset before the filter."""
    lungo = tmp_path / "girato.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30000/1001:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(lungo)],
        check=True,
    )
    frames, start = 138, 4.575
    length = frames / 30
    pezzo = tmp_path / "pezzo.mov"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{start:.6f}", "-i", str(lungo),
         "-vf", "setpts=PTS-STARTPTS,fps=30", "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-video_track_timescale", "30000",
         "-af", f"aresample=48000,asetpts=PTS-STARTPTS,apad,atrim=end={length:.6f}",
         "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(pezzo)],
        check=True,
    )

    video, audio = workspace.stream_durations(pezzo)

    assert video == pytest.approx(length, abs=1 / 600)
    assert audio == pytest.approx(length, abs=1 / 600)
    workspace._refuse_drift(pezzo, 30)
