"""The worker loop.

One process claims a job, runs it and reports the outcome. Claiming is atomic,
so several workers can run side by side; a job whose worker dies is returned to
the queue by `reap_stalled` and retried.

Phase 1 ships the handlers that need no new infrastructure — validate, sync and
render of a workspace project. The remaining kinds are declared and fail with an
explicit message instead of pretending to work.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from skyground.config import Settings, get_settings
from skyground.core.workspace import Workspace
from skyground.db import build_engine
from skyground.db.models import MODE_WORKSPACE, Project, RenderJob
from skyground.errors import StudioError
from skyground.services import assets as asset_service
from skyground.services import jobs as job_service
from skyground.services.documents import DocumentService
from skyground.storage import ObjectStorage, build_storage

logger = logging.getLogger("skyground.worker")

#: How a preview proxy is encoded. Half height keeps scrubbing responsive and
#: the file small enough to stream over a hotel connection.
PROXY_FORMATS = {
    "h264": (
        "source.mp4",
        "video/mp4",
        ("-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"),
    ),
    "vp9": (
        "source.webm",
        "video/webm",
        ("-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-deadline", "realtime",
         "-cpu-used", "8", "-row-mt", "1", "-pix_fmt", "yuv420p", "-c:a", "libopus", "-b:a", "96k"),
    ),
}


class NotSupported(StudioError):
    """Raised by a handler that cannot run yet: the job fails without retrying."""


class Worker:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory=None,
        workspace: Workspace | None = None,
        storage: ObjectStorage | None = None,
        name: str | None = None,
        kinds: tuple[str, ...] | None = None,
    ):
        self.settings = settings or get_settings()
        self.workspace = workspace or Workspace(self.settings.workspace_root)
        self.storage = storage or build_storage(self.settings)
        self.session_factory = session_factory or sessionmaker(
            bind=build_engine(self.settings), expire_on_commit=False, future=True
        )
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.kinds = kinds
        self.running = False
        self.handlers: dict[str, Callable[[Session, RenderJob, Project], dict]] = {
            "analyze": self.handle_analyze,
            "full": self.handle_full,
            "validate": self.handle_validate,
            "sync": self.handle_sync,
            "render": self.handle_render,
            "proxy": self.handle_proxy,
            "transcribe": self.handle_unsupported,
        }

    # ------------------------------------------------------------------- loop

    def run_forever(self) -> None:
        self.running = True
        self._install_signal_handlers()
        logger.info("worker %s avviato", self.name)
        self.reap_orphans()
        last_reap = 0.0
        while self.running:
            if time.monotonic() - last_reap > 60:
                self.reap()
                last_reap = time.monotonic()
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)
        logger.info("worker %s fermato", self.name)

    def run_once(self) -> bool:
        """Run at most one job. Returns True when something was claimed."""
        with self.session_factory() as session:
            job = job_service.claim(session, self.name, kinds=self.kinds)
            session.commit()
            if job is None:
                return False
            job_id, kind = job.id, job.kind

        with self.session_factory() as session:
            job = session.get(RenderJob, job_id)
            project = session.get(Project, job.project_id)
            try:
                result = self.execute(session, job, project)
                job_service.succeed(session, job, result)
                logger.info("job %s (%s) completato", job_id, kind)
            except NotSupported as error:
                job_service.fail(session, job, str(error), retry=False)
                logger.warning("job %s non supportato: %s", job_id, error)
            except Exception as error:  # noqa: BLE001 - the queue records every failure
                job_service.fail(session, job, f"{type(error).__name__}: {error}")
                logger.exception("job %s fallito", job_id)
            session.commit()
        return True

    def reap_orphans(self) -> int:
        """Requeue what a previous worker of this host left running."""
        with self.session_factory() as session:
            count = job_service.reap_orphans(session, self.name)
            session.commit()
        if count:
            logger.warning("%s job lasciati a metà da un worker precedente rimessi in coda", count)
        return count

    def reap(self) -> int:
        with self.session_factory() as session:
            count = job_service.reap_stalled(
                session, timeout_seconds=self.settings.worker_job_timeout_seconds
            )
            session.commit()
        if count:
            logger.warning("%s job bloccati rimessi in coda", count)
        return count

    def execute(self, session: Session, job: RenderJob, project: Project) -> dict:
        handler = self.handlers.get(job.kind)
        if handler is None:
            raise NotSupported(f"tipo di job sconosciuto: {job.kind}")
        return handler(session, job, project)

    # --------------------------------------------------------------- handlers

    def handle_analyze(self, session: Session, job: RenderJob, project: Project) -> dict:
        """Transcribe the raw take and store what was heard.

        The slow step of the whole product — minutes of audio through a model —
        so it happens once, in the worker, and every later proposal is rebuilt
        from the stored result in milliseconds.
        """
        from skyground.analysis import pipeline
        from skyground.analysis.transcription import build_transcriber
        from skyground.services import cuts

        source = self._source_for(session, project, job)
        analysis = pipeline.analyze(
            source,
            transcriber=build_transcriber(self.settings),
            language=(project.settings or {}).get("language", "it"),
            source_label=str(job.payload.get("source") or source.name),
        )
        cuts.save_analysis(session, project, analysis)
        plan = cuts.propose(session, project, model=_editor_model())
        return {
            "words": len(analysis.words),
            "duration": analysis.duration,
            "provider": analysis.provider,
            "proposal": plan.stats(),
        }

    def _source_for(self, session: Session, project: Project, job: RenderJob):
        """Find the raw take: in the checkout, or pulled down from storage."""
        import pathlib
        import tempfile

        relative = job.payload.get("source") or self._timeline_source(session, project)
        if self.workspace.exists(project.slug):
            candidate = self.workspace.project_dir(project.slug) / relative
            if candidate.is_file():
                return candidate

        from skyground.services import assets as asset_service

        key = asset_service.object_key(project, relative)
        if not self.storage.exists(key):
            raise NotSupported(
                f"il girato {relative} non è né nel workspace né nello storage"
            )
        target = pathlib.Path(tempfile.gettempdir()) / "skyground" / project.slug / relative
        return self.storage.download(key, target)

    def _timeline_source(self, session: Session, project: Project) -> str:
        timeline = DocumentService(session, self.workspace).read(project, "timeline.json")
        return (timeline.content or {}).get("source") or "assets/raw.mov"

    def handle_full(self, session: Session, job: RenderJob, project: Project) -> dict:
        """From raw footage to a rendered film, with nobody in between.

        Each step already existed as its own command, and each was a thing
        somebody had to remember to run, in order, with the right flags. This
        is that order, written down once: hear the footage, let the editor
        decide, write the cut into the documents, rebuild picture and sound,
        lay the captions, check everything, render, publish. The report says
        what the editor decided and what was left out, so the film that comes
        back is never a surprise.
        """
        from skyground.analysis.editor import build_model
        from skyground.services import cuts

        self._require_workspace(project)
        report: dict = {}
        if not cuts.has_analysis(session, project) or job.payload.get("reanalyze"):
            report["analysis"] = self.handle_analyze(session, job, project)
        override = job.payload.get("editor") or None
        model = build_model(self.settings, override) if self.settings.cut_engine == "editor" else None
        plan = cuts.propose(session, project, model=model, keep_answers=not job.payload.get("fresh", True))
        if plan.open_questions:
            raise StudioError(
                f"il montatore ha lasciato {len(plan.open_questions)} domande aperte: "
                "impostato per chiedere, non per decidere"
            )
        report["editor"] = {
            "model": plan.editor.get("model") or "euristico",
            "segments": len(plan.segments),
            "duration": round(plan.stats()["outputDuration"], 3),
            "cuts": sum(1 for q in plan.questions if q.id.startswith("edit:")),
            "reviews": plan.editor.get("reviews", []),
            "summary": plan.editor.get("summary", ""),
        }
        applied = cuts.apply(session, project, self.workspace)
        report["applied"] = {"clips": applied["clips"], "duration": applied["duration"],
                             "dropped": applied.get("dropped", [])}
        session.commit()  # the documents are on disk and in the database before the long steps
        self.workspace.build_source(project.slug)
        self.workspace.sync(project.slug)
        problems = self.workspace.validate(project.slug)
        if problems:
            raise StudioError("progetto non valido dopo il montaggio:\n- " + "\n- ".join(problems))
        if job.payload.get("render", True):
            report["render"] = self.handle_render(session, job, project)
        return report

    def handle_validate(self, session: Session, job: RenderJob, project: Project) -> dict:
        problems = DocumentService(session, self.workspace).problems(project)
        return {"problems": problems, "valid": not problems}

    def handle_sync(self, session: Session, job: RenderJob, project: Project) -> dict:
        self._require_workspace(project)
        return self.workspace.sync(project.slug)

    def handle_render(self, session: Session, job: RenderJob, project: Project) -> dict:
        """Render and publish the result.

        A render is never overwritten: the file is versioned, and the object key
        in storage carries the same version.
        """
        self._require_workspace(project)
        problems = self.workspace.validate(project.slug)
        if problems:
            raise StudioError("progetto non valido:\n- " + "\n- ".join(problems))
        output = self.workspace.render(project.slug)
        key = asset_service.object_key(project, f"renders/{output.name}")
        stored = self.storage.put_file(key, output, "video/mp4")
        asset_service.register(
            session,
            project,
            key=key,
            kind="render",
            size=stored.size,
            sha256=stored.sha256,
            content_type="video/mp4",
            meta={"job": job.id},
        )
        return {"output": output.name, "key": key, "size": stored.size}

    def handle_proxy(self, session: Session, job: RenderJob, project: Project) -> dict:
        """Make a copy the browser can actually play.

        The camera original is HEVC in a .mov: no browser will decode it, so
        without this the editor shows a black rectangle and the timeline is
        useless. The proxy is half resolution H.264 with a moved index, which is
        what makes scrubbing feel immediate.
        """
        import pathlib
        import subprocess
        import tempfile

        from skyground.core.workspace import find_ffmpeg
        from skyground.services import assets as asset_service

        source = self._source_for(session, project, job)
        name, media_type, encoder = PROXY_FORMATS.get(
            self.settings.proxy_codec, PROXY_FORMATS["h264"]
        )
        with tempfile.TemporaryDirectory(prefix="skyground-proxy-") as temporary:
            output = pathlib.Path(temporary) / name
            subprocess.run(
                [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                 "-vf", "scale=-2:960", *encoder, str(output)],
                check=True,
            )
            key = asset_service.object_key(project, f"proxy/{name}")
            stored = self.storage.put_file(key, output, media_type)

        asset_service.register(
            session, project, key=key, kind="proxy", size=stored.size,
            sha256=stored.sha256, content_type="video/mp4", meta={"job": job.id},
        )
        return {"key": key, "size": stored.size}

    def handle_unsupported(self, session: Session, job: RenderJob, project: Project) -> dict:
        raise NotSupported(
            f"il job '{job.kind}' arriva con la pipeline della fase 3; "
            "in questa fase non viene eseguito"
        )

    # ---------------------------------------------------------------- helpers

    def _require_workspace(self, project: Project) -> None:
        if project.storage_mode != MODE_WORKSPACE:
            raise NotSupported(
                "questo job richiede un progetto nel workspace; i progetti gestiti "
                "vengono materializzati dalla pipeline della fase 3"
            )
        if not self.workspace.exists(project.slug):
            raise NotSupported(f"il worker non vede il progetto {project.slug} sul disco")

    def _install_signal_handlers(self) -> None:
        def stop(_signum, _frame):
            logger.info("arresto richiesto, chiudo dopo il job corrente")
            self.running = False

        for name in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(name, stop)
            except ValueError:  # pragma: no cover - not the main thread
                pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = get_settings()
    settings.check_deployable()
    Worker(settings=settings).run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def _editor_model():
    from skyground.analysis.editor import build_model
    from skyground.config import get_settings

    settings = get_settings()
    return build_model(settings) if settings.cut_engine == "editor" else None
