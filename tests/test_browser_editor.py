"""The editor, driven the way a person drives it.

The cut engine had 250 passing tests while the screen in front of it was, in the
owner's words, impossible to use: the video never showed a frame, and choosing
between two takes meant reading two paragraphs because nothing could play one.
None of that is visible from the API, so these tests use the tool.

They run against a real server and a real Chromium, with a short synthetic video
standing in for the footage — the shapes are what matter here, not the content.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from tests.conftest import PROJECT_ID

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="i test nel browser richiedono playwright"
)

FFMPEG = shutil.which("ffmpeg") or os.environ.get("FFMPEG_BIN", "")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def planned(client, registered_project, sign_in, settings, tmp_path):
    """A project with a proposal on the table and a playable video behind it."""
    if not FFMPEG:
        pytest.skip("serve ffmpeg per fabbricare un video riproducibile")

    words = []
    # Two attempts at the same line, then something else: enough for the engine
    # to raise a take question, which is the case the screen exists for.
    script = [
        ("Buongiorno", 0.5), ("a", 1.0), ("tutti", 1.3),
        ("Buongiorno", 3.0), ("a", 3.5), ("tutti", 3.8),
        ("oggi", 6.0), ("parliamo", 6.4), ("di", 6.9), ("montaggio", 7.2),
    ]
    for text, at in script:
        words.append({"t": at, "end": at + 0.35, "s": text, "p": 0.95})
    fixture = tmp_path / "parole.json"
    fixture.write_text(json.dumps(words), encoding="utf-8")

    # VP9, not H.264: the Chromium that ships with playwright carries no
    # licensed codecs, so an .mp4 proxy decodes to nothing here. The application
    # supports both, which is why `SKYGROUND_PROXY_CODEC` exists.
    video = tmp_path / "raw.webm"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=#1a2b3c:s=270x480:d=9",
         "-f", "lavfi", "-i", "sine=frequency=200:duration=9",
         "-c:v", "libvpx-vp9", "-b:v", "200k", "-pix_fmt", "yuv420p",
         "-c:a", "libopus", "-shortest", str(video)],
        check=True,
    )
    # The source the worker analyses, and the browser playable copy beside it.
    for key in (f"projects/{PROJECT_ID}/assets/raw.mov", f"projects/{PROJECT_ID}/proxy/source.webm"):
        destination = pathlib.Path(settings.storage_root) / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(video, destination)

    sign_in("editor@skyground.online")
    return {"fixture": fixture, "video": video}


@pytest.fixture
def editor(app, planned, settings, monkeypatch):
    """The application on a real port, with the fixture transcriber wired in."""
    from dataclasses import replace

    from skyground.config import set_settings

    set_settings(replace(settings, transcription_provider=f"fixture:{planned['fixture']}"))
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("il server di prova non è partito")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    set_settings(settings)


@pytest.fixture
def page(editor, client):
    """A browser on the editor, already signed in and with a plan to look at."""
    if client.get(f"{editor}/app").status_code == 503:  # pragma: no cover
        pytest.skip("editor non compilato: `npm run build` in app/")
    client.post(f"/api/projects/{PROJECT_ID}/cut/analyze", json={})
    from skyground.worker.runner import Worker

    Worker().run_once()
    client.post(f"/api/projects/{PROJECT_ID}/cut/propose")

    with playwright_api.sync_playwright() as playwright:
        executable = os.environ.get("SKYGROUND_BROWSER_EXECUTABLE") or None
        try:
            browser = playwright.chromium.launch(executable_path=executable)
        except Exception as cause:  # pragma: no cover
            pytest.skip(f"nessun browser disponibile: {cause}")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        # The session the API handed the test client, given to the browser.
        cookie = client.cookies.get("skyground_session")
        if cookie:
            host = editor.replace("http://", "").split(":")[0]
            context.add_cookies(
                [{"name": "skyground_session", "value": cookie, "domain": host, "path": "/"}]
            )
        page = context.new_page()
        page.goto(f"{editor}/app", wait_until="networkidle")
        page.wait_for_selector(".decide", timeout=15_000)
        yield page
        context.close()
        browser.close()


def _video_state(page) -> dict:
    return page.evaluate(
        "() => { const v = document.querySelector('video');"
        " return v ? {t: v.currentTime, paused: v.paused, w: v.videoWidth} : null; }"
    )


# --------------------------------------------------------------------- il video


def test_the_video_shows_a_frame_before_anything_is_pressed(page):
    """A black rectangle reads as a broken tool, so the player parks itself on
    the first frame the edit keeps instead of on nothing."""
    page.wait_for_function(
        "() => { const v = document.querySelector('video');"
        " return v && v.videoWidth > 0 && v.currentTime > 0; }",
        timeout=15_000,
    )
    state = _video_state(page)
    assert state["w"] > 0  # decoded: the browser can actually play this file
    assert state["t"] > 0


def test_the_edit_plays(page):
    page.click(".transport .play")
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v && !v.paused; }",
        timeout=10_000,
    )
    assert _video_state(page)["paused"] is False


# ---------------------------------------------------------------- le decisioni


def test_one_question_at_a_time_and_the_count_says_where_you_are(page):
    assert "1 di" in page.locator(".decide .eyebrow").inner_text()
    # Not six cards stacked into a page two screens tall.
    assert page.locator(".decide h2").count() == 1


def test_every_take_can_be_heard_before_choosing(page):
    """The point of the screen: you cannot pick between two takes by reading
    them. Each option that names a piece of source can play exactly that."""
    listen = page.locator(".option button.listen")
    assert listen.count() >= 2

    listen.first.click()
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v && !v.paused; }",
        timeout=10_000,
    )
    playing_at = _video_state(page)["t"]

    detail = page.locator(".option").first.locator("small").inner_text()
    start = float(detail.split("–")[0])
    assert abs(playing_at - start) < 1.5, f"riproduce {playing_at}, non {start}"


def test_choosing_an_option_removes_the_question(page):
    before = page.locator(".decide .eyebrow").inner_text()
    page.locator(".option button.choose").first.click()
    page.wait_for_function(
        f"() => document.querySelector('.decide .eyebrow').innerText !== {before!r}",
        timeout=15_000,
    )
    assert page.locator(".decide").count() == 1


def test_the_whole_tool_fits_on_one_screen(page):
    """Deciding means looking at the video and at the options together. A page
    that scrolls means scrolling away from one of them."""
    metrics = page.evaluate(
        "() => ({doc: document.documentElement.scrollHeight, win: window.innerHeight,"
        " wide: document.documentElement.scrollWidth > window.innerWidth})"
    )
    assert metrics["wide"] is False
    assert metrics["doc"] <= metrics["win"] + 40, f"la pagina sborda: {metrics}"
