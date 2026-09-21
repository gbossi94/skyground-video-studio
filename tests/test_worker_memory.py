"""Memory, which on a two gigabyte instance is the thing that breaks.

The studio's web service and its worker share one container. A job that holds
a gigabyte takes the whole studio down with it, so the worker waits when the
ceiling is close, and the transcription — the biggest of them — runs in a
child process that gives its memory back by ending.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from skyground.analysis import transcription
from skyground.analysis.models import Word
from skyground.errors import StudioError
from skyground.worker import runner


def cgroup(tmp_path: pathlib.Path, limit: str, used: str) -> pathlib.Path:
    (tmp_path / "memory.max").write_text(limit)
    (tmp_path / "memory.current").write_text(used)
    return tmp_path


def test_free_memory_is_read_from_the_container_limit(tmp_path):
    assert runner.memory_free(cgroup(tmp_path, "2147483648", "1200000000")) == 947483648
    # No limit set, or no cgroup at all: unknowable, and the guard stands aside.
    assert runner.memory_free(cgroup(tmp_path, "max", "1200000000")) is None
    assert runner.memory_free(tmp_path / "nowhere") is None


def test_a_worker_waits_instead_of_starting_a_heavy_job_with_no_room(monkeypatch, settings, session_factory, workspace):
    worker = runner.Worker(settings=settings, session_factory=session_factory, workspace=workspace, name="t:1")
    monkeypatch.setattr(runner, "memory_free", lambda: 300 * 1024 * 1024)
    assert worker.short_of_memory() is True
    monkeypatch.setattr(runner, "memory_free", lambda: 1200 * 1024 * 1024)
    assert worker.short_of_memory() is False
    # Where the limit cannot be read — a Mac — the guard stands aside.
    monkeypatch.setattr(runner, "memory_free", lambda: None)
    assert worker.short_of_memory() is False


def test_a_worker_that_only_does_light_jobs_never_waits(monkeypatch, settings, session_factory, workspace):
    worker = runner.Worker(settings=settings, session_factory=session_factory, workspace=workspace,
                           name="t:2", kinds=("validate", "sync"))
    monkeypatch.setattr(runner, "memory_free", lambda: 10 * 1024 * 1024)
    assert worker.short_of_memory() is False


def test_the_transcription_runs_apart_and_its_words_come_back(monkeypatch, tmp_path):
    seen = {}

    def fake_run(command, input, capture_output, text):
        seen["command"] = command
        seen["request"] = json.loads(input)
        words = [Word(t=0.0, end=0.4, s="ciao", p=0.9).as_dict()]
        return subprocess.CompletedProcess(command, 0, json.dumps({"words": words}), "")

    monkeypatch.setattr(transcription.subprocess if hasattr(transcription, "subprocess") else subprocess, "run", fake_run)
    monkeypatch.setattr("subprocess.run", fake_run)
    transcriber = transcription.LocalWhisperTranscriber(model="small", beam_size=1, cpu_threads=2)
    words = transcriber.transcribe(tmp_path / "audio.wav", language="it")

    assert [word.s for word in words] == ["ciao"]
    assert seen["command"][1:] == ["-m", "skyground.analysis.transcribe_once"]
    assert seen["request"]["beam_size"] == 1 and seen["request"]["cpu_threads"] == 2


def test_a_child_that_dies_says_why(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 137, "", "Killed: out of memory"),
    )
    with pytest.raises(StudioError, match="out of memory"):
        transcription.LocalWhisperTranscriber().transcribe(tmp_path / "audio.wav")


def test_a_job_left_by_a_worker_on_a_dead_host_goes_back_in_the_queue(session_factory, registered_project):
    """On Render the host name changes at every deploy, so «same host» never
    matched and a preview sat an hour marked as running."""
    from skyground.db.models import JOB_QUEUED, JOB_RUNNING, RenderJob
    from skyground.services import jobs as job_service

    with session_factory() as db:
        from skyground.db.models import Project

        live = db.get(Project, registered_project["project"].id)
        job = job_service.enqueue(db, live, kind="proxy")
        job.status = JOB_RUNNING
        job.locked_by = "vecchio-host:12"
        db.commit()
        job_id = job.id

        assert job_service.reap_orphans(db, "nuovo-host:34", sole=False) == 0
        assert db.get(RenderJob, job_id).status == JOB_RUNNING

        assert job_service.reap_orphans(db, "nuovo-host:34", sole=True) == 1
        db.commit()
        assert db.get(RenderJob, job_id).status == JOB_QUEUED
