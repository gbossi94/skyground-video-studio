"""The way in, exercised in a real browser.

Everything else in this suite talks to the API directly, which is exactly how a
freshly deployed studio managed to answer `{"required": true}` on
`/api/auth/setup` while showing the visitor a page with no form on it: the panel
decided whether to open the form by comparing the error *message*, and the API
says "autenticazione richiesta" where the panel expected "Autenticazione
richiesta". Green tests, unusable instance.

So these tests drive Chromium against a real server. They are the only ones that
can see a hidden form.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest
import uvicorn

from tests.conftest import PASSWORD, PROJECT_ID

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="i test nel browser richiedono playwright"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def studio_url(app):
    """The application on a real port, because a browser cannot use TestClient."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - only on a broken host
            raise RuntimeError("il server di prova non è partito")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page(studio_url):
    with playwright_api.sync_playwright() as playwright:
        # `SKYGROUND_BROWSER_EXECUTABLE` points at a Chromium already on the
        # machine, for hosts that ship one instead of letting playwright
        # download its own matching build.
        executable = os.environ.get("SKYGROUND_BROWSER_EXECUTABLE") or None
        try:
            browser = playwright.chromium.launch(executable_path=executable)
        except Exception as cause:  # pragma: no cover - depends on the host
            pytest.skip(f"nessun browser disponibile: {cause}")
        context = browser.new_context()
        page = context.new_page()
        yield page
        context.close()
        browser.close()


# --------------------------------------------------------------- claiming it


def test_a_fresh_studio_shows_the_form_that_creates_the_first_account(page, studio_url):
    page.goto(studio_url)
    form = page.locator("#signin-form")
    form.wait_for(state="visible", timeout=10_000)
    assert page.locator("#signin-intro").is_visible()
    assert "non ha ancora un account" in page.locator("#signin-intro").inner_text()
    assert page.locator("#signin-submit").inner_text() == "Crea l'account"


def test_the_first_visitor_signs_up_and_lands_on_the_project(page, studio_url, workspace):
    page.goto(studio_url)
    page.locator("#signin-form").wait_for(state="visible", timeout=10_000)
    page.fill("#email", "gabriele@skyground.online")
    page.fill("#password", PASSWORD)
    page.click("#signin-submit")

    # The reload that follows lands on a signed-in panel with the project in it.
    page.wait_for_selector(".project", timeout=10_000)
    assert PROJECT_ID in page.locator(".project").first.get_attribute("data-id")
    assert "gabriele@skyground.online" in page.locator("#identity").inner_text()
    assert page.locator("#signin").is_hidden()


def test_a_studio_that_already_has_an_account_asks_for_the_password(page, studio_url, make_user):
    make_user("gabriele@skyground.online")
    page.goto(studio_url)
    page.locator("#signin-form").wait_for(state="visible", timeout=10_000)
    # Not the claiming form: the door is closed, this one only lets people in.
    assert page.locator("#signin-intro").is_hidden()
    assert page.locator("#signin-submit").inner_text() == "Entra"


def test_a_wrong_password_is_reported_on_the_form(page, studio_url, make_user):
    make_user("gabriele@skyground.online")
    page.goto(studio_url)
    page.locator("#signin-form").wait_for(state="visible", timeout=10_000)
    page.fill("#email", "gabriele@skyground.online")
    page.fill("#password", "sbagliata-ma-lunga")
    page.click("#signin-submit")
    # `to_contain_text` polls: the message arrives with the failed request, not
    # with the click.
    playwright_api.expect(page.locator("#signin-message")).to_contain_text(
        "credenziali non valide", timeout=10_000
    )
    assert page.locator("#signin-form").is_visible()


# ------------------------------------------------------------- the editor too


def test_the_editor_sends_a_visitor_without_a_session_to_the_form(page, studio_url):
    """`/app` has no form of its own: it must not be a dead end."""
    if page.request.get(f"{studio_url}/app").status == 503:
        pytest.skip("editor non compilato: `npm run build` in app/")
    page.goto(f"{studio_url}/app")
    page.locator("#signin-form").wait_for(state="visible", timeout=15_000)
    assert page.url.rstrip("/") == studio_url
