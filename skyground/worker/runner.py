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
            "validate": self.handle_validate,
            "sync": self.handle_sync,
            "render": self.handle_render,
            "proxy": self.handle_unsupported,
            "transcribe": self.handle_unsupported,
        }

    # ------------------------------------------------------------------- loop

    def run_forever(self) -> None:
        self.running = True
        self._install_signal_handlers()
        logger.info("worker %s avviato", self.name)
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
