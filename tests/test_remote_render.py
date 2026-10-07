"""Rendering a production project on this machine.

What matters is what the command must never do: touch the checkout's own
documents or built files, render against a raw take that is not the one
production cut, or publish a film that breaks the project's rules.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from skyground import remote
from skyground.core.workspace import Workspace
from skyground.errors import StudioError


@pytest.fixture
def checkout(tmp_path: pathlib.Path) -> Workspace:
    base = tmp_path / "checkout" / "projects" / "film"
    (base / "assets").mkdir(parents=True)
    (base / "composition").mkdir()
    (base / "renders").mkdir()
    (base / "assets" / "raw.mov").write_bytes(b"raw")
    (base / "composition" / "angle-call.mp4").write_bytes(b"angle")
    (base / "composition" / "source.mp4").write_bytes(b"old cut")
    (base / "composition" / "soundtrack.m4a").write_bytes(b"old mix")
    (base / "composition" / "index.html").write_text("<html></html>")
    (base / "renders" / "approved.mp4").write_bytes(b"approved")
    (base / "preview.mp4").write_bytes(b"preview")
    (base / "timeline.json").write_text(json.dumps({"source": "assets/raw.mov", "clips": []}))
    return Workspace(tmp_path / "checkout")


def test_the_copy_links_the_media_and_leaves_out_what_belongs_to_another_cut(checkout, tmp_path):
    staged = remote.stage(checkout, "film", tmp_path / "copy")
    base = staged.project_dir("film")
    original = checkout.project_dir("film")

    assert (base / "assets" / "raw.mov").stat().st_ino == (original / "assets" / "raw.mov").stat().st_ino
    assert (base / "composition" / "angle-call.mp4").read_bytes() == b"angle"
    # Built for the checkout's cut: rebuilt in the copy, never reused.
    assert not (base / "composition" / "source.mp4").exists()
    assert not (base / "composition" / "soundtrack.m4a").exists()
    assert not (base / "renders").exists()
    assert not (base / "preview.mp4").exists()
    # Text is copied, not linked: writing the copy's documents leaves the checkout's alone.
    (base / "timeline.json").write_text("{}")
    (base / "composition" / "index.html").write_text("changed")
    assert json.loads((original / "timeline.json").read_text())["source"] == "assets/raw.mov"
    assert (original / "composition" / "index.html").read_text() == "<html></html>"


class FakeRemote:
    def __init__(self, server):
        self.server = server
        self.uploaded = None

    def login(self, email, password):
        return {"email": email}

    def cut(self, project):
        return {"plan": {"sourceDuration": 99.0}}

    def document(self, project, name):
        return {"source": "assets/raw.mov", "clips": [{"start": 0, "end": 1}]} if name == "timeline.json" else {}

    def upload_render(self, project, path):
        self.uploaded = path
        return {"key": f"projects/{project}/renders/{path.name}"}


def test_a_raw_take_that_is_not_the_one_production_cut_is_refused(checkout, monkeypatch):
    monkeypatch.setattr(remote, "Remote", FakeRemote)
    monkeypatch.setattr(remote, "probe_duration", lambda path: 42.0)
    with pytest.raises(StudioError, match="non è lo stesso file"):
        remote.render_remote(checkout, "film", server="https://x", email="a@b", password="p", say=lambda *_: None)


def test_the_composition_names_the_media_it_would_play_without(tmp_path):
    base = tmp_path / "projects" / "film"
    (base / "composition").mkdir(parents=True)
    (base / "project.json").write_text(json.dumps({"files": {"composition": "composition/index.html"}}))
    (base / "composition" / "source.mp4").write_bytes(b"x")
    (base / "composition" / "index.html").write_text(
        '<video id="raw" src="source.mp4"></video>'
        '<video id="angle-call" class="clip" muted src="angle-call.mp4"></video>'
        '<audio id="mix" src="soundtrack.m4a"></audio>'
        '<video src="https://cdn.example/x.mp4"></video>'
    )
    assert Workspace(tmp_path).missing_media("film") == ["angle-call.mp4", "soundtrack.m4a"]
