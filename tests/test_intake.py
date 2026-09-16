"""From a raw video to a project, and from a project to a film, by itself.

Until this the studio could edit a project somebody had laid out by hand and
could not start one — every render so far ran on the one project checked into
the repository. These pin the way in: a project laid out around a raw file, a
neutral composition the captions land on, and the single job that runs the
whole chain with nobody in between.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

from skyground.core.workspace import Workspace
from skyground.errors import ValidationError

FFMPEG = shutil.which("ffmpeg") or os.environ.get("FFMPEG_BIN", "")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="serve ffmpeg per fabbricare un video")


def make_video(path: pathlib.Path, seconds: float = 6.0, *, codec: str = "libx264") -> pathlib.Path:
    ext = "webm" if codec == "libvpx-vp9" else "mp4"
    path = path.with_suffix("." + ext)
    audio = ["-c:a", "libopus"] if codec == "libvpx-vp9" else ["-c:a", "aac"]
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=270x480:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
         "-c:v", codec, "-pix_fmt", "yuv420p", *audio, "-shortest", str(path)],
        check=True,
    )
    return path


# ---------------------------------------------------------------- il progetto


@needs_ffmpeg
def test_a_project_is_laid_out_around_a_raw_video(tmp_path):
    ws = Workspace(tmp_path)
    video = make_video(tmp_path / "girato")

    created = ws.create_project("nuovo-01", "Nuovo", video)

    assert created["duration"] == pytest.approx(6.0, abs=0.1)
    base = tmp_path / "projects" / "nuovo-01"
    for name in ("project.json", "timeline.json", "captions.json", "cards.json", "angles.json",
                 "brand.json", "audio.json", "composition/index.html", "assets/raw.mp4"):
        assert (base / name).exists(), name
    manifest = json.loads((base / "project.json").read_text())
    assert manifest["canvas"] == {"width": 1080, "height": 1920, "fps": 30, "duration": 6.0}
    assert manifest["source"]["width"] == 270  # what the file is, kept apart from what the film is
    timeline = json.loads((base / "timeline.json").read_text())
    assert timeline["source"] == "assets/raw.mp4"
    assert ws.validate("nuovo-01") == []


@needs_ffmpeg
def test_the_same_id_cannot_be_laid_out_twice(tmp_path):
    ws = Workspace(tmp_path)
    video = make_video(tmp_path / "girato")
    ws.create_project("nuovo-01", "Nuovo", video)
    with pytest.raises(ValidationError, match="esiste già"):
        ws.create_project("nuovo-01", "Nuovo", video)


def test_a_bad_id_is_refused_before_anything_is_written(tmp_path):
    ws = Workspace(tmp_path)
    with pytest.raises(ValidationError):
        ws.create_project("../fuori", "x", tmp_path / "nessuno.mp4")
    assert not (tmp_path / "projects").exists()


@needs_ffmpeg
def test_brand_audio_and_music_come_from_the_template_project(tmp_path, workspace_root):
    """A new film sounds and looks like the studio's others."""
    ws = Workspace(workspace_root)
    template = workspace_root / "projects" / "beauty-centers-growth-01"
    (template / "composition" / "music.mp3").write_bytes(b"non e musica ma basta")
    video = make_video(tmp_path / "girato")

    ws.create_project("nuovo-02", "Nuovo", video, template_project="beauty-centers-growth-01")

    base = workspace_root / "projects" / "nuovo-02"
    assert json.loads((base / "brand.json").read_text()) == json.loads((template / "brand.json").read_text())
    assert (base / "composition" / "music.mp3").read_bytes() == b"non e musica ma basta"


# ------------------------------------------------------------ la composizione


@needs_ffmpeg
def test_the_neutral_composition_survives_sync_with_no_cards_twice(tmp_path):
    """The first sync used to eat the card marker the second one looks for, and
    a global replace of the duration «1» would have rewritten every 1 in the file."""
    ws = Workspace(tmp_path)
    ws.create_project("nuovo-03", "Nuovo", make_video(tmp_path / "girato"))

    ws.sync("nuovo-03")
    ws.sync("nuovo-03")

    html = (tmp_path / "projects" / "nuovo-03" / "composition" / "index.html").read_text()
    assert re.findall(r'data-duration="([^"]+)"', html) == ["6.0", "6.0", "6.0"]
    assert "1080px" in html  # a «1» that must not have been touched
    assert 'id="card-nessuna"' in html


def test_captions_start_where_the_composition_says(workspace):
    from skyground.core.workspace import caption_groups

    words = [{"t": 1.0, "end": 1.3, "s": "ciao"}, {"t": 9.0, "end": 9.3, "s": "dopo"}]
    assert [g[0]["s"] for g in caption_groups(words, [], 20.0)] == ["ciao", "dopo"]
    assert [g[0]["s"] for g in caption_groups(words, [], 20.0, captions_from=8.43)] == ["dopo"]


# --------------------------------------------------------------------- il job


@needs_ffmpeg
def test_the_full_job_runs_the_whole_chain(settings, session_factory, workspace, make_user, tmp_path, monkeypatch):
    """Hear, decide, cut, rebuild, lay the captions, check — with a fixture
    transcriber and the heuristic editor, and the render switched off."""
    from dataclasses import replace

    from skyground.config import set_settings
    from skyground.services import jobs as job_service
    from skyground.services import projects as project_service
    from skyground.worker.runner import Worker

    words = [{"t": at, "end": at + 0.3, "s": text, "p": 0.95} for text, at in [
        ("Buongiorno", 0.5), ("a", 1.0), ("tutti", 1.3),
        ("Buongiorno", 2.6), ("a", 3.1), ("tutti", 3.4),
        ("oggi", 4.2), ("parliamo", 4.6), ("di", 5.0), ("montaggio", 5.3)]]
    fixture = tmp_path / "parole.json"
    fixture.write_text(json.dumps(words))
    tuned = replace(settings, transcription_provider=f"fixture:{fixture}", cut_engine="heuristic")
    set_settings(tuned)

    video = make_video(tmp_path / "girato")
    workspace.create_project("nuovo-04", "Nuovo", video)
    owner = make_user("owner@skyground.online")
    with session_factory() as session:
        from skyground.db.models import User as _User
        project = project_service.register_workspace_project(
            session, workspace, "nuovo-04", owner=session.get(_User, owner.id))
        job = job_service.enqueue(session, project, kind="full", payload={"render": False}, actor=owner)
        session.commit()
        job_id = job.id

    worker = Worker(settings=tuned, session_factory=session_factory, workspace=workspace)
    assert worker.run_once() is True

    with session_factory() as session:
        from skyground.db.models import RenderJob
        done = session.get(RenderJob, job_id)
        assert done.status == "succeeded", done.error
        report = done.result
    assert report["editor"]["segments"] >= 1
    assert report["applied"]["clips"] == report["editor"]["segments"]
    base = workspace.project_dir("nuovo-04")
    assert (base / "composition" / "source.mp4").exists()
    assert (base / "composition" / "soundtrack.m4a").exists()
    assert json.loads((base / "captions.json").read_text()), "nessun sottotitolo scritto"
    assert workspace.validate("nuovo-04") == []
    set_settings(settings)


# --------------------------------------------------------------------- l'API


@needs_ffmpeg
def test_a_project_is_created_from_the_video_in_one_request(client, sign_in, make_user, workspace, tmp_path):
    make_user("editor@skyground.online")
    sign_in("editor@skyground.online")
    video = make_video(tmp_path / "girato")

    response = client.put(
        "/api/projects/nuovo-05",
        content=video.read_bytes(),
        headers={"Content-Type": "video/mp4", "X-Skyground-Name": "Nuovo dal video",
                 "X-Skyground-Filename": video.name},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["name"] == "Nuovo dal video"
    assert payload["source"]["duration"] == pytest.approx(6.0, abs=0.1)
    assert workspace.exists("nuovo-05")
    assert (workspace.project_dir("nuovo-05") / "assets" / "raw.mp4").stat().st_size == video.stat().st_size

    queued = client.post("/api/projects/nuovo-05/cut/full", json={"render": False})
    assert queued.status_code == 200, queued.text
    assert queued.json()["kind"] == "full"


# ------------------------------------------------------------ render veloce


def test_a_plain_film_is_recognised_and_a_card_takes_it_back_to_the_browser(tmp_path):
    ws = Workspace(tmp_path)
    ws.create_project("nuovo-06", "Nuovo", make_video(tmp_path / "girato"))
    assert ws.is_plain("nuovo-06") is True

    base = tmp_path / "projects" / "nuovo-06"
    (base / "cards.json").write_text('[{"id": "x", "a": 1.0, "b": 2.0, "label": "X", "body": "x", "kind": "split"}]')
    assert ws.is_plain("nuovo-06") is False


def test_the_hand_made_composition_is_never_plain(workspace):
    """It has the intro scenes, the angles, the cards: only a browser draws it."""
    assert workspace.is_plain("beauty-centers-growth-01") is False


def test_captions_are_written_as_subtitles_where_the_template_puts_them():
    from skyground.core.workspace import _ass

    groups = [[{"t": 1.0, "end": 1.3, "s": "ciao"}, {"t": 1.3, "end": 1.6, "s": "a"}],
              [{"t": 1.6, "end": 2.0, "s": "tutti"}]]
    text = _ass(groups, 1080, 1920, 10.0)
    assert "PlayResY: 1920" in text
    assert "Dialogue: 0,0:00:00.96,0:00:01.56,Cap,,0,0,0,,ciao a" in text  # ends where the next begins
    assert "tutti" in text
    assert "Fontsize" in text.split("\n")[7] and ",63," in text  # the template's 63px


@needs_ffmpeg
def test_a_plain_film_renders_with_ffmpeg_in_seconds(tmp_path):
    """The whole point: no browser, no screenshots, the same picture and the
    same sound that `build_source` already rendered — with the captions, the
    mark and the bar burned in."""
    import time

    ws = Workspace(tmp_path)
    ws.create_project("nuovo-07", "Nuovo", make_video(tmp_path / "girato"))
    base = tmp_path / "projects" / "nuovo-07"
    (base / "captions.json").write_text(json.dumps([
        {"t": 0.5, "end": 0.9, "s": "Buongiorno"}, {"t": 0.9, "end": 1.2, "s": "a"}, {"t": 1.2, "end": 1.6, "s": "tutti"}]))
    ws.build_source("nuovo-07")
    ws.sync("nuovo-07")

    started = time.monotonic()
    output = ws.render("nuovo-07")

    assert time.monotonic() - started < 120
    assert output.exists() and (base / "renders" / "latest.mp4").exists()
    from skyground.core.workspace import stream_durations

    video, audio = stream_durations(output)
    assert video == pytest.approx(6.0, abs=1 / 30) and abs(video - audio) < 1 / 30
    probe = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(output)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert probe == "1080,1920"
    assert (base / "composition" / "captions.ass").exists()
