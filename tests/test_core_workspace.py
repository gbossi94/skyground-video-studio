"""The editorial core keeps behaving exactly as it did before the refactor."""

from __future__ import annotations

import pytest

from skyground.core.workspace import (
    Workspace,
    caption_groups,
    content_digest,
    validate_documents,
)
from skyground.errors import NotFound, ValidationError
from tests.conftest import PROJECT_ID


def test_the_real_project_validates(workspace: Workspace):
    assert workspace.validate(PROJECT_ID) == []


def test_list_projects_keeps_its_payload(workspace: Workspace):
    projects = workspace.list_projects()
    assert [project["id"] for project in projects] == [PROJECT_ID]
    project = projects[0]
    assert project["path"] == f"projects/{PROJECT_ID}"
    assert project["previewAvailable"] is False  # preview.mp4 is not in Git
    assert project["canvas"]["width"] == 1080
    assert project["canvas"]["height"] == 1920
    assert project["canvas"]["fps"] == 30


def test_sync_is_idempotent_on_the_committed_composition(workspace: Workspace):
    """Syncing a project that is already in sync must not change a single byte."""
    composition = workspace.project_dir(PROJECT_ID) / "composition" / "index.html"
    before = composition.read_bytes()
    result = workspace.sync(PROJECT_ID)
    assert composition.read_bytes() == before
    assert result == {"cards": 18, "words": 409, "captionGroups": 64}


def test_sync_rewrites_cards_and_captions(workspace: Workspace):
    cards = workspace.read_document(PROJECT_ID, "cards.json")
    cards[0]["label"] = "NUOVA ETICHETTA"
    workspace.write_document(PROJECT_ID, "cards.json", cards)
    workspace.sync(PROJECT_ID)
    html = (workspace.project_dir(PROJECT_ID) / "composition" / "index.html").read_text("utf-8")
    assert "NUOVA ETICHETTA" in html


def test_captions_never_overlap_a_motion_card(workspace: Workspace):
    """The binding Skyground rule: a card and a caption never speak at once."""
    cards = workspace.read_document(PROJECT_ID, "cards.json")
    captions = workspace.read_document(PROJECT_ID, "captions.json")
    duration = workspace.read_document(PROJECT_ID, "project.json")["canvas"]["duration"]
    groups = caption_groups(captions, cards, duration)
    windows = [(float(card["a"]), float(card["b"])) for card in cards]
    for group in groups:
        for word in group:
            assert not any(start <= float(word["t"]) < end for start, end in windows)


def test_validation_catches_a_broken_timeline(workspace: Workspace):
    timeline = workspace.read_document(PROJECT_ID, "timeline.json")
    timeline["clips"][2]["output_start"] = 99.0
    workspace.write_document(PROJECT_ID, "timeline.json", timeline)
    problems = workspace.validate(PROJECT_ID)
    assert any("output_start" in problem for problem in problems)


def test_validation_catches_overlapping_cards(workspace: Workspace):
    cards = workspace.read_document(PROJECT_ID, "cards.json")
    cards[1]["a"] = cards[0]["a"]
    workspace.write_document(PROJECT_ID, "cards.json", cards)
    assert any("sovrapposta" in problem for problem in workspace.validate(PROJECT_ID))


def test_validation_catches_an_angle_past_the_end(workspace: Workspace):
    angles = workspace.read_document(PROJECT_ID, "angles.json")
    angles[1]["start"] = 999.0
    workspace.write_document(PROJECT_ID, "angles.json", angles)
    assert any("angle" in problem for problem in workspace.validate(PROJECT_ID))


def test_validate_documents_matches_the_filesystem_validator(workspace: Workspace):
    """The document validator is what makes a managed project checkable."""
    documents = {
        name.removesuffix(".json"): workspace.read_document(PROJECT_ID, name)
        for name in ("project.json", "timeline.json", "cards.json", "captions.json", "angles.json")
    }
    missing = [
        problem for problem in workspace.validate(PROJECT_ID) if problem.startswith("file mancante")
    ]
    assert validate_documents(documents, missing_files=missing) == workspace.validate(PROJECT_ID)


def test_media_path_refuses_to_escape_the_project(workspace: Workspace):
    with pytest.raises(NotFound):
        workspace.media_path(PROJECT_ID, "../../../etc/passwd")


def test_project_id_must_be_a_slug(workspace: Workspace):
    with pytest.raises(ValidationError):
        workspace.project_dir("../secrets")
    with pytest.raises(ValidationError):
        workspace.document_path(PROJECT_ID, "assets.lock.json")


def test_documents_are_written_in_the_repository_format(workspace: Workspace):
    """Two spaces, UTF-8, trailing newline: the diff must stay readable."""
    brand = workspace.read_document(PROJECT_ID, "brand.json")
    workspace.write_document(PROJECT_ID, "brand.json", brand)
    text = (workspace.project_dir(PROJECT_ID) / "brand.json").read_text("utf-8")
    assert text.endswith("}\n")
    assert '\n  "name": "Skyground"' in text
    assert "Skyground è" not in text or "\\u" not in text


def test_content_digest_ignores_key_order(workspace: Workspace):
    assert content_digest({"a": 1, "b": 2}) == content_digest({"b": 2, "a": 1})
    assert content_digest([1, 2]) != content_digest([2, 1])


def test_render_never_reuses_an_existing_filename(workspace: Workspace):
    first = workspace.render_output_path(PROJECT_ID, version="20260101-120000")
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"render approvato")
    second = workspace.render_output_path(PROJECT_ID, version="20260101-120000")
    assert second != first
    assert not second.exists()
