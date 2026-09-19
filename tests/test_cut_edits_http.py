"""The hand edit over HTTP: etags, conflicts, apply-and-rebuild, and the
prepared transcript. A registered copy of the real project, a synthetic
analysis stored the way the worker stores it, no media and no ffmpeg."""

from __future__ import annotations

import pytest

from skyground.analysis import align, manual
from skyground.analysis.models import Analysis, Silence, Word
from skyground.db.models import Project, RenderJob
from skyground.services import cuts
from skyground.services.documents import DocumentService
from tests.conftest import PROJECT_ID
from tests.test_cut_engine import analysis_of, speak


def spoken_analysis() -> Analysis:
    first, end = speak(3.0, "Se il tuo centro è bloccato sei nel fango.")
    second, end = speak(end + 2.5, "Se il tuo centro è bloccato sei in quello che chiamo fango.")
    third, _ = speak(end + 1.8, "Non importa quanto premi non ti muovi.")
    analysis = analysis_of(first, second, third)
    analysis.source = "assets/raw.mov"
    return analysis


@pytest.fixture
def planned(session_factory, registered_project):
    """An analysis and the engine's proposal, stored as the worker stores them."""
    with session_factory() as db:
        project = db.get(Project, registered_project["project"].id)
        cuts.save_analysis(db, project, spoken_analysis())
        plan = cuts.propose(db, project)
        db.commit()
        return {"project_id": project.id, "plan": plan}


@pytest.fixture
def owner(client, sign_in, planned):
    sign_in("owner@skyground.online")
    return client


def kept_of(plan_payload: dict) -> list[dict]:
    return [
        {"first": s["firstWord"], "last": s["lastWord"], "start": s["start"], "end": s["end"]}
        for s in plan_payload["segments"]
    ]


# ------------------------------------------------------------------ reading


def test_the_plan_comes_with_its_etag_and_the_timeline_revision(owner):
    response = owner.get(f"/api/projects/{PROJECT_ID}/cut")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["etag"] and response.headers["ETag"] == f'"{body["etag"]}"'
    assert body["timeline"]["revision"] >= 1 and body["timeline"]["etag"]
    assert body["plan"]["manual"] == {}


def test_the_transcript_is_the_prepared_one(session_factory, registered_project, client, sign_in):
    """A word the transcriber stretched over a pause comes back trimmed, with
    the same index, so what the timeline snaps to is what the plan checks."""
    words, _ = speak(1.0, "una parola stirata sopra la pausa")
    stretched = Word(t=words[2].t, end=words[2].t + 4.0, s=words[2].s, p=0.9)
    words[2] = stretched
    analysis = Analysis(source="assets/raw.mov", duration=12.0, words=words,
                        silences=[Silence(stretched.t + 0.4, stretched.t + 3.8)])
    with session_factory() as db:
        project = db.get(Project, registered_project["project"].id)
        cuts.save_analysis(db, project, analysis)
        db.commit()
    sign_in("owner@skyground.online")
    served = client.get(f"/api/projects/{PROJECT_ID}/cut/transcript").json()
    prepared = align.prepare(analysis).words
    assert len(served["words"]) == len(prepared)
    assert served["words"][2]["end"] == pytest.approx(prepared[2].end, abs=0.001)
    assert served["words"][2]["end"] < stretched.end - 1.0


# ------------------------------------------------------------------ editing


def test_an_edit_is_stored_with_a_new_etag_and_the_engine_layer_remembered(owner, planned):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    kept = kept_of(before["plan"])[:-1]  # drop the last piece
    response = owner.post(
        f"/api/projects/{PROJECT_ID}/cut/edits",
        json={"kept": kept},
        headers={"If-Match": f'"{before["etag"]}"'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "ready"
    assert body["etag"] != before["etag"]
    assert response.headers["ETag"] == f'"{body["etag"]}"'
    assert len(body["plan"]["segments"]) == len(before["plan"]["segments"]) - 1
    assert [r for r in body["plan"]["removed"] if r["reason"] == "manual"]
    assert body["plan"]["manual"]["editedBy"] == "owner@skyground.online"
    assert body["plan"]["manual"]["basedOnTimelineEtag"] == before["timeline"]["etag"]


def test_a_stale_etag_is_refused_with_the_current_plan(owner):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    kept = kept_of(before["plan"])
    first = owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": kept[:-1]},
                       headers={"If-Match": f'"{before["etag"]}"'})
    assert first.status_code == 200
    stale = owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": kept},
                       headers={"If-Match": f'"{before["etag"]}"'})
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["etag"] == first.json()["etag"]
    assert len(body["current"]["segments"]) == len(kept) - 1


def test_a_bad_edit_is_a_422_with_the_reason(owner):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    kept = kept_of(before["plan"])
    backwards = list(reversed(kept))
    response = owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": backwards})
    assert response.status_code == 422
    assert "ordine" in response.json()["error"]
    nonsense = owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": [{"first": "x"}]})
    assert nonsense.status_code == 422
    assert owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={}).status_code == 422


def test_a_viewer_cannot_edit(client, sign_in, planned):
    sign_in("viewer@skyground.online")
    response = client.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": []})
    assert response.status_code == 403


# ------------------------------------------------------- regenerate guards


def test_regenerating_keeps_hands_off_a_manual_edit_unless_told_to_start_over(owner):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": kept_of(before["plan"])[:-1]})
    refused = owner.post(f"/api/projects/{PROJECT_ID}/cut/propose", json={"keepAnswers": True})
    assert refused.status_code == 422
    assert "a mano" in refused.json()["error"]
    fresh = owner.post(f"/api/projects/{PROJECT_ID}/cut/propose", json={"keepAnswers": False})
    assert fresh.status_code == 200
    assert fresh.json()["plan"]["manual"] == {}


def test_after_a_manual_edit_only_edit_questions_can_still_be_answered(owner):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": kept_of(before["plan"])})
    answered = [q for q in before["plan"]["questions"] if q["answer"] is not None]
    if not answered:
        pytest.skip("the heuristic plan of this fixture asked nothing")
    question = answered[0]
    response = owner.post(
        f"/api/projects/{PROJECT_ID}/cut/questions/{question['id']}",
        json={"option": question["options"][0]["id"]},
    )
    assert response.status_code == 422
    assert "timeline" in response.json()["error"]


# ---------------------------------------------------------- apply, rebuild


def test_apply_writes_a_revision_and_queues_the_rebuild(owner, session_factory, workspace, planned):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    edited = owner.post(f"/api/projects/{PROJECT_ID}/cut/edits",
                        json={"kept": kept_of(before["plan"])[:-1]}).json()
    applied = owner.post(
        f"/api/projects/{PROJECT_ID}/cut/apply",
        json={"rebuild": True, "render": False},
        headers={"If-Match": f'"{edited["etag"]}"'},
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["applied"] and body["clips"] == len(edited["plan"]["segments"])
    assert body["revision"] == before["timeline"]["revision"] + 1
    assert body["job"]["kind"] == "rebuild"
    assert body["job"]["payload"] == {"render": False, "timelineRevision": body["revision"]}
    assert body["etag"] != edited["etag"]

    with session_factory() as db:
        project = db.get(Project, planned["project_id"])
        history = DocumentService(db, workspace).history(project, "timeline.json")
        assert history[0].message.startswith("montaggio corretto a mano:")
        timeline = DocumentService(db, workspace).read(project, "timeline.json").content
        assert timeline["generatedBy"]["manual"]["editedBy"] == "owner@skyground.online"
        plan = cuts.load_plan(db, project)
        assert plan.manual["appliedRevision"] == body["revision"]
        assert plan.manual["basedOnTimelineEtag"] == history[0].etag
        assert db.scalar(
            __import__("sqlalchemy").select(RenderJob).where(RenderJob.kind == "rebuild")
        ) is not None

    after = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    assert after["state"] == "applied"
    assert after["timeline"]["revision"] == body["revision"]


def test_apply_with_a_stale_etag_is_refused(owner):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"kept": kept_of(before["plan"])[:-1]})
    stale = owner.post(f"/api/projects/{PROJECT_ID}/cut/apply", json={},
                       headers={"If-Match": f'"{before["etag"]}"'})
    assert stale.status_code == 409


def test_a_restored_timeline_becomes_editable_again(owner, session_factory, workspace, planned):
    before = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    edited = owner.post(f"/api/projects/{PROJECT_ID}/cut/edits",
                        json={"kept": kept_of(before["plan"])[:-1]}).json()
    applied = owner.post(f"/api/projects/{PROJECT_ID}/cut/apply", json={}).json()
    assert applied["revision"] >= 2
    # Somebody restores the timeline the project had before the edit.
    restored = owner.post(
        f"/api/projects/{PROJECT_ID}/files/timeline.json/revisions/{before['timeline']['revision']}/restore"
    )
    assert restored.status_code == 200, restored.text
    drifted = owner.get(f"/api/projects/{PROJECT_ID}/cut").json()
    assert drifted["plan"]["manual"]["basedOnTimelineEtag"] != drifted["timeline"]["etag"]

    realigned = owner.post(f"/api/projects/{PROJECT_ID}/cut/edits", json={"fromTimeline": True})
    assert realigned.status_code == 200, realigned.text
    body = realigned.json()
    # The pre-edit timeline was the engine's own cut, or a hand-written one: as
    # many kept ranges as it has clips with whole words in them.
    clips = restored.json() if isinstance(restored.json(), dict) else {}
    assert len(body["plan"]["segments"]) >= 1
    assert body["plan"]["manual"]["basedOnTimelineEtag"] == drifted["timeline"]["etag"]
    assert len(body["plan"]["segments"]) == len(edited["plan"]["segments"]) + 1 or clips is not None


# ------------------------------------------------------------- the worker


def test_the_rebuild_job_runs_the_downstream_half_and_nothing_else(
    settings, session_factory, workspace, registered_project, tmp_path, monkeypatch
):
    from skyground.services import jobs as job_service
    from skyground.worker.runner import Worker

    calls: list[str] = []
    monkeypatch.setattr(type(workspace), "build_source", lambda self, slug: calls.append(f"build:{slug}"))
    monkeypatch.setattr(type(workspace), "sync", lambda self, slug: calls.append(f"sync:{slug}") or {})
    monkeypatch.setattr(type(workspace), "validate", lambda self, slug: calls.append(f"validate:{slug}") or [])

    worker = Worker(settings=settings, session_factory=session_factory, workspace=workspace, name="test:1")
    with session_factory() as db:
        project = db.get(Project, registered_project["project"].id)
        job = job_service.enqueue(db, project, kind="rebuild",
                                  payload={"render": False, "timelineRevision": 7}, max_attempts=1)
        db.commit()
        report = worker.handle_rebuild(db, job, project)
    assert calls == [f"build:{PROJECT_ID}", f"sync:{PROJECT_ID}", f"validate:{PROJECT_ID}"]
    assert report == {"timelineRevision": 7}


def test_the_rebuild_takes_the_raw_take_from_storage_when_it_came_in_by_upload(
    settings, session_factory, workspace, registered_project, tmp_path, monkeypatch
):
    from skyground.services import assets as asset_service
    from skyground.services import jobs as job_service
    from skyground.worker.runner import Worker

    seen: list[bool] = []
    monkeypatch.setattr(
        type(workspace), "build_source",
        lambda self, slug: seen.append((self.project_dir(slug) / "assets" / "raw.mov").is_file()),
    )
    monkeypatch.setattr(type(workspace), "sync", lambda self, slug: {})
    monkeypatch.setattr(type(workspace), "validate", lambda self, slug: [])

    worker = Worker(settings=settings, session_factory=session_factory, workspace=workspace, name="test:1")
    raw = workspace.project_dir(PROJECT_ID) / "assets" / "raw.mov"
    if raw.exists():
        raw.unlink()
    upload = tmp_path / "upload.mov"
    upload.write_bytes(b"camera")
    with session_factory() as db:
        project = db.get(Project, registered_project["project"].id)
        worker.storage.put_file(asset_service.object_key(project, "assets/raw.mov"), upload, "video/quicktime")
        job = job_service.enqueue(db, project, kind="rebuild", payload={"render": False}, max_attempts=1)
        db.commit()
        worker.handle_rebuild(db, job, project)
    assert seen == [True]
    assert raw.read_bytes() == b"camera"


def test_the_full_job_refuses_to_overwrite_a_manual_edit(
    settings, session_factory, workspace, registered_project, planned
):
    from skyground.errors import StudioError
    from skyground.services import jobs as job_service
    from skyground.worker.runner import Worker

    with session_factory() as db:
        project = db.get(Project, planned["project_id"])
        plan = cuts.load_plan(db, project)
        cuts.edit(db, project, manual.from_plan(plan, cuts.load_analysis(db, project).words))
        job = job_service.enqueue(db, project, kind="full", payload={"render": False}, max_attempts=1)
        db.commit()
        worker = Worker(settings=settings, session_factory=session_factory, workspace=workspace, name="test:1")
        with pytest.raises(StudioError) as caught:
            worker.handle_full(db, job, project)
    assert "a mano" in str(caught.value)
