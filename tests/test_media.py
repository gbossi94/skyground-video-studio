"""The media the timeline draws: peaks, thumbnail sheets, the manifest, and
the routes that hand them to the browser. Pure arithmetic tested with synthetic
audio; ffmpeg itself only where it is installed."""

from __future__ import annotations

import io
import json
import math
import shutil
import struct

import pytest

from skyground.analysis import media
from skyground.db.models import Project, RenderJob
from skyground.services import assets as asset_service
from skyground.storage import build_storage
from tests.conftest import PROJECT_ID

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="serve ffmpeg per fabbricare un video")


def pcm(samples: list[int]) -> io.BytesIO:
    return io.BytesIO(struct.pack(f"<{len(samples)}h", *samples))


# ------------------------------------------------------------------- peaks


def test_one_peak_per_window_and_the_tail_counts_as_one():
    silence = [0] * (media.PEAK_WINDOW * 3 + 7)
    peaks = media.peaks_from_pcm(pcm(silence))
    assert len(peaks) == 4
    assert set(peaks) == {0}


def test_a_peak_is_the_loudest_magnitude_of_its_window_scaled_to_a_byte():
    window = media.PEAK_WINDOW
    quiet = [100] * window
    loud = [0] * (window - 1) + [-32767]
    half = [16384] * window
    peaks = media.peaks_from_pcm(pcm(quiet + loud + half))
    assert list(peaks) == [0, 255, 127]


def test_a_sine_is_read_the_same_whatever_the_chunking():
    seconds = 2.5
    total = int(media.PEAK_SAMPLE_RATE * seconds)
    sine = [int(30000 * math.sin(2 * math.pi * 440 * i / media.PEAK_SAMPLE_RATE)) for i in range(total)]
    reference = media.peaks_from_pcm(pcm(sine))
    assert len(reference) == math.ceil(total / media.PEAK_WINDOW) == 250
    assert max(reference) >= 230

    class Dribble(io.BytesIO):
        """Returns oddly sized reads, including torn samples."""

        def read(self, size=-1):
            return super().read(min(size, 37) if size and size > 0 else size)

    assert media.peaks_from_pcm(Dribble(struct.pack(f"<{total}h", *sine))) == reference


# -------------------------------------------------------------- thumbnails


@pytest.mark.parametrize(
    ("duration", "count", "sheets"),
    [(0.0, 1, 1), (0.4, 1, 1), (599.0, 599, 1), (600.0, 600, 1), (600.5, 601, 2), (1800.0, 1800, 3)],
)
def test_thumbnail_layout(duration, count, sheets):
    layout = media.thumbnail_layout(duration)
    assert (layout["count"], layout["sheets"]) == (count, sheets)
    assert layout["perSheet"] == layout["columns"] * layout["rows"] == 600


def test_the_filter_tiles_portrait_thumbnails_one_a_second():
    assert media.thumbnail_filter() == (
        "fps=1,scale=68:120:force_original_aspect_ratio=increase,crop=68:120,tile=20x30"
    )


# ---------------------------------------------------------------- manifest


def test_the_manifest_carries_every_key_and_the_grid():
    index = media.manifest(
        proxy_key="projects/x/proxy/source.mp4", codec="h264", duration=115.933,
        peaks_key="projects/x/proxy/peaks.u8", thumb_keys=["projects/x/proxy/thumbs-000.jpg"],
        source_sha256="abc",
    )
    assert index["proxy"] == {"key": "projects/x/proxy/source.mp4", "codec": "h264", "fps": 30, "gop": 15}
    assert index["peaks"] == {"key": "projects/x/proxy/peaks.u8", "rate": 100}
    assert index["thumbs"]["keys"] == ["projects/x/proxy/thumbs-000.jpg"]
    assert index["thumbs"]["count"] == 116 and index["thumbs"]["fps"] == 1
    assert index["duration"] == 115.933 and index["sourceSha256"] == "abc"


# ------------------------------------------------------------------ routes


@pytest.fixture
def stored_manifest(settings, session_factory, registered_project):
    storage = build_storage(settings)
    with session_factory() as db:
        project = db.get(Project, registered_project["project"].id)
        keys = {
            name: asset_service.object_key(project, f"proxy/{name}")
            for name in ("source.mp4", "peaks.u8", "thumbs-000.jpg", "index.json")
        }
        storage.put(keys["source.mp4"], b"\x00" * 16, "video/mp4")
        storage.put(keys["peaks.u8"], bytes(range(100)), "application/octet-stream")
        storage.put(keys["thumbs-000.jpg"], b"\xff\xd8\xff", "image/jpeg")
        index = media.manifest(proxy_key=keys["source.mp4"], codec="h264", duration=1.0,
                               peaks_key=keys["peaks.u8"], thumb_keys=[keys["thumbs-000.jpg"]])
        storage.put(keys["index.json"], json.dumps(index).encode(), "application/json")
        db.commit()
    return keys


def test_the_media_route_signs_every_artefact_with_the_long_ttl(client, sign_in, stored_manifest):
    sign_in("owner@skyground.online")
    response = client.get(f"/api/projects/{PROJECT_ID}/cut/media")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True
    assert body["fps"] == 30 and body["gop"] == 15
    assert "expires=" in body["proxy"] and "expires=" in body["peaks"]
    assert len(body["thumbs"]["urls"]) == 1 and body["thumbs"]["count"] == 1
    assert body["expiresAt"] > 0
    # The proxy is signed for hours, the raw take for minutes.
    proxy_expiry = int(body["proxy"].split("expires=")[1].split("&")[0])
    raw = client.get(f"/api/projects/{PROJECT_ID}/cut").json()
    assert raw["media"]["ready"] is True
    assert raw["analysis"] is None or raw["analysis"].get("proxyUrl") in (None, body["proxy"])
    assert proxy_expiry - body["expiresAt"] <= 1 and body["ttl"] == 14400


def test_without_a_manifest_the_media_route_says_so_and_can_queue_the_job(
    client, sign_in, registered_project, session_factory
):
    sign_in("owner@skyground.online")
    missing = client.get(f"/api/projects/{PROJECT_ID}/cut/media")
    assert missing.status_code == 200
    assert missing.json() == {"ready": False, "job": None}
    queued = client.post(f"/api/projects/{PROJECT_ID}/cut/media")
    assert queued.status_code == 200, queued.text
    assert queued.json()["kind"] == "proxy"
    again = client.get(f"/api/projects/{PROJECT_ID}/cut/media").json()
    assert again["ready"] is False and again["job"]["kind"] == "proxy"
    # Asking twice does not queue twice while one is pending.
    assert client.post(f"/api/projects/{PROJECT_ID}/cut/media").json()["id"] == queued.json()["id"]
    with session_factory() as db:
        assert db.query(RenderJob).filter(RenderJob.kind == "proxy").count() == 1


# ------------------------------------------------------------------ ffmpeg


@needs_ffmpeg
def test_the_proxy_job_makes_all_three_artefacts_with_short_gops(
    settings, session_factory, workspace, registered_project, tmp_path
):
    import subprocess

    from skyground.services import jobs as job_service
    from skyground.worker.runner import Worker

    source = workspace.project_dir(PROJECT_ID) / "assets" / "raw.mov"
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
        check=True,
    )
    worker = Worker(settings=settings, session_factory=session_factory, workspace=workspace, name="t:1")
    with session_factory() as db:
        project = db.get(Project, registered_project["project"].id)
        job = job_service.enqueue(db, project, kind="proxy", payload={"source": "assets/raw.mov"})
        db.commit()
        result = worker.handle_proxy(db, job, project)
        db.commit()
    storage = build_storage(settings)
    index = json.loads(storage.get(result["manifest"]))
    assert index["thumbs"]["count"] == 3 and len(index["thumbs"]["keys"]) == 1
    peaks = storage.get(index["peaks"]["key"])
    assert 295 <= len(peaks) <= 305 and max(peaks) > 100

    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(storage.get(index["proxy"]["key"]))
    frames = subprocess.run(
        [FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=key_frame", "-of", "csv=p=0", str(proxy)],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    keyframes = [i for i, flag in enumerate(frames) if flag == "1"]
    assert keyframes[:4] == [0, 15, 30, 45]
