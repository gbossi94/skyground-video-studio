"""The render queue and the worker that drains it."""

from __future__ import annotations

from datetime import timedelta

import pytest

from skyground.db.models import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    Project,
    User,
    utcnow,
)
from skyground.errors import ValidationError
from skyground.services import jobs as job_service
from skyground.storage.local import LocalObjectStorage
from skyground.worker import Worker
from tests.conftest import PROJECT_ID


@pytest.fixture
def project(session, registered_project) -> Project:
    return session.get(Project, registered_project["project"].id)


@pytest.fixture
def editor(session, registered_project) -> User:
    return session.get(User, registered_project["editor"].id)


@pytest.fixture
def worker(settings, session_factory, workspace, tmp_path) -> Worker:
    return Worker(
        settings=settings,
        session_factory=session_factory,
        workspace=workspace,
        storage=LocalObjectStorage(tmp_path / "objects", secret_key="test"),
        name="worker-di-test",
    )


def test_a_job_starts_queued(session, project, editor):
    job = job_service.enqueue(session, project, kind="render", actor=editor)
    assert job.status == JOB_QUEUED
    assert job.attempts == 0
    assert job.requested_by == editor.id


def test_an_unknown_kind_is_refused(session, project):
    with pytest.raises(ValidationError):
        job_service.enqueue(session, project, kind="mandaEmail")


def test_claiming_takes_one_job_and_marks_it_running(session, project):
    first = job_service.enqueue(session, project, kind="validate")
    second = job_service.enqueue(session, project, kind="validate")
    session.commit()

    claimed = job_service.claim(session, "worker-1")
    assert claimed.id == first.id
    assert claimed.status == JOB_RUNNING
    assert claimed.attempts == 1
    assert claimed.locked_by == "worker-1"
    assert job_service.claim(session, "worker-2").id == second.id
    assert job_service.claim(session, "worker-3") is None


def test_priority_comes_first(session, project):
    job_service.enqueue(session, project, kind="validate")
    urgent = job_service.enqueue(session, project, kind="validate", priority=10)
    session.commit()
    assert job_service.claim(session, "worker-1").id == urgent.id


def test_a_job_scheduled_for_later_is_not_claimed(session, project):
    job = job_service.enqueue(session, project, kind="validate")
    job.available_at = utcnow() + timedelta(minutes=5)
    session.flush()
    assert job_service.claim(session, "worker-1") is None


def test_a_worker_only_claims_the_kinds_it_handles(session, project):
    job_service.enqueue(session, project, kind="render")
    transcription = job_service.enqueue(session, project, kind="transcribe")
    session.commit()
    assert job_service.claim(session, "worker-1", kinds=("transcribe",)).id == transcription.id


def test_a_failure_is_retried_with_a_delay(session, project):
    job = job_service.enqueue(session, project, kind="validate")
    session.commit()
    job_service.claim(session, "worker-1")
    job_service.fail(session, job, "ffmpeg è uscito con codice 1")

    assert job.status == JOB_QUEUED
    assert job.available_at > utcnow()
    assert job.locked_by is None
    assert "ffmpeg" in job.error


def test_retries_stop_at_the_limit(session, project):
    job = job_service.enqueue(session, project, kind="validate", max_attempts=2)
    session.commit()
    for _attempt in range(2):
        job.available_at = utcnow()
        session.flush()
        job_service.claim(session, "worker-1")
        job_service.fail(session, job, "errore")
    assert job.status == JOB_FAILED
    assert job.attempts == 2
    assert job.finished_at is not None


def test_a_failure_can_refuse_to_retry(session, project):
    job = job_service.enqueue(session, project, kind="validate")
    session.commit()
    job_service.claim(session, "worker-1")
    job_service.fail(session, job, "non supportato", retry=False)
    assert job.status == JOB_FAILED


def test_success_records_the_result(session, project):
    job = job_service.enqueue(session, project, kind="validate")
    session.commit()
    job_service.claim(session, "worker-1")
    job_service.succeed(session, job, {"problems": []})
    assert job.status == JOB_SUCCEEDED
    assert job.result == {"problems": []}
    assert job.error is None


def test_a_finished_job_cannot_be_cancelled(session, project):
    job = job_service.enqueue(session, project, kind="validate")
    session.commit()
    job_service.claim(session, "worker-1")
    job_service.succeed(session, job)
    with pytest.raises(ValidationError):
        job_service.cancel(session, job)


def test_a_queued_job_can_be_cancelled(session, project):
    job = job_service.enqueue(session, project, kind="validate")
    job_service.cancel(session, job)
    assert job.status == JOB_CANCELLED


def test_a_job_held_by_a_dead_worker_goes_back_to_the_queue(session, project):
    job = job_service.enqueue(session, project, kind="validate")
    session.commit()
    job_service.claim(session, "worker-caduto")
    job.locked_at = utcnow() - timedelta(hours=3)
    session.flush()

    assert job_service.reap_stalled(session, timeout_seconds=3600) == 1
    assert job.status == JOB_QUEUED
    assert "non ha risposto" in job.error


def test_a_job_left_running_by_a_restarted_worker_goes_back_at_once(session, project):
    """The container restarted mid-job: the new worker on the same host
    requeues it right away instead of waiting for the hour-long stall."""
    job = job_service.enqueue(session, project, kind="validate")
    other = job_service.enqueue(session, project, kind="validate")
    session.commit()
    job_service.claim(session, "studio-1:32")
    job_service.claim(session, "studio-2:32")  # another host: not ours to touch
    session.flush()

    assert job_service.reap_orphans(session, "studio-1:57") == 1
    assert job.status == JOB_QUEUED
    assert "riavviato" in job.error
    assert other.status == JOB_RUNNING
    # the new worker's own jobs are left alone
    job_service.claim(session, "studio-1:57")
    assert job_service.reap_orphans(session, "studio-1:57") == 0


def test_the_worker_requeues_its_predecessor_s_jobs_when_it_starts(worker, session_factory, project):
    with session_factory() as db:
        job = job_service.enqueue(db, db.get(Project, project.id), kind="validate")
        db.commit()
        job_service.claim(db, worker.name.rsplit(":", 1)[0] + ":1")
        db.commit()
        job_id = job.id

    assert worker.reap_orphans() == 1
    with session_factory() as db:
        assert job_service.get(db, job_id).status == JOB_QUEUED


# ------------------------------------------------------------------- worker


def test_the_worker_runs_a_validation_job(worker, session_factory, project):
    with session_factory() as db:
        job = job_service.enqueue(db, db.get(Project, project.id), kind="validate")
        db.commit()
        job_id = job.id

    assert worker.run_once() is True
    with session_factory() as db:
        job = job_service.get(db, job_id)
        assert job.status == JOB_SUCCEEDED
        assert job.result == {"problems": [], "valid": True}


def test_the_worker_runs_a_sync_job(worker, session_factory, project, workspace):
    with session_factory() as db:
        job_service.enqueue(db, db.get(Project, project.id), kind="sync")
        db.commit()
    assert worker.run_once() is True
    with session_factory() as db:
        job = job_service.list_for_project(db, db.get(Project, project.id))[0]
        assert job.status == JOB_SUCCEEDED
        assert job.result["cards"] == 18


def test_an_empty_queue_is_not_an_error(worker):
    assert worker.run_once() is False


def test_a_job_that_cannot_run_yet_fails_clearly_and_is_not_retried(
    worker, session_factory, project
):
    with session_factory() as db:
        job = job_service.enqueue(db, db.get(Project, project.id), kind="transcribe")
        db.commit()
        job_id = job.id

    worker.run_once()
    with session_factory() as db:
        job = job_service.get(db, job_id)
        assert job.status == JOB_FAILED
        assert job.attempts == 1
        assert "fase 3" in job.error


def test_a_render_of_an_invalid_project_fails_instead_of_producing_a_file(
    worker, session_factory, project, workspace
):
    cards = workspace.read_document(PROJECT_ID, "cards.json")
    cards[0]["b"] = cards[0]["a"] - 5
    workspace.write_document(PROJECT_ID, "cards.json", cards)
    with session_factory() as db:
        job = job_service.enqueue(db, db.get(Project, project.id), kind="render")
        db.commit()
        job_id = job.id

    worker.run_once()
    with session_factory() as db:
        job = job_service.get(db, job_id)
        assert job.status in (JOB_QUEUED, JOB_FAILED)
        assert "progetto non valido" in job.error


# --------------------------------------------------------------- over HTTP


def test_jobs_over_http(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    created = client.post(f"/api/projects/{PROJECT_ID}/jobs", json={"kind": "validate"})
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert created.json()["status"] == JOB_QUEUED

    listed = client.get(f"/api/projects/{PROJECT_ID}/jobs").json()
    assert [item["id"] for item in listed] == [job_id]
    assert client.get(f"/api/projects/{PROJECT_ID}/jobs/{job_id}").json()["kind"] == "validate"


def test_a_viewer_cannot_queue_a_render(client, registered_project, sign_in):
    sign_in("viewer@skyground.online")
    response = client.post(f"/api/projects/{PROJECT_ID}/jobs", json={"kind": "render"})
    assert response.status_code == 403


def test_only_an_owner_cancels(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    job_id = client.post(f"/api/projects/{PROJECT_ID}/jobs", json={"kind": "validate"}).json()["id"]
    assert client.post(f"/api/projects/{PROJECT_ID}/jobs/{job_id}/cancel").status_code == 403

    client.post("/api/auth/logout")
    sign_in("owner@skyground.online")
    cancelled = client.post(f"/api/projects/{PROJECT_ID}/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == JOB_CANCELLED
