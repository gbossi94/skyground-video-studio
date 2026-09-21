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
from skyground.db.models import JOB_RUNNING, MODE_WORKSPACE, Project, RenderJob
from skyground.errors import StudioError
from skyground.services import assets as asset_service
from skyground.services import jobs as job_service
from skyground.services.documents import DocumentService
from skyground.storage import ObjectStorage, build_storage

logger = logging.getLogger("skyground.worker")

#: How a preview proxy is encoded. Half height keeps scrubbing responsive and
#: the file small enough to stream over a hotel connection. A keyframe every
#: fifteen frames, closed groups, no scene-cut keyframes: a seek anywhere
#: decodes half a second at most, which is what makes frame-by-frame
#: scrubbing in the browser feel immediate. The frame rate is pinned to the
#: studio's grid so frame N in the editor is frame N in the render.
PROXY_FORMATS = {
    "h264": (
        "source.mp4",
        "video/mp4",
        ("-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
         "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
         "-x264-params", "open-gop=0",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"),
    ),
    "vp9": (
        "source.webm",
        "video/webm",
        ("-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-deadline", "realtime",
         "-cpu-used", "8", "-row-mt", "1", "-pix_fmt", "yuv420p",
         "-g", "15", "-keyint_min", "15",
         "-c:a", "libopus", "-b:a", "96k"),
    ),
}


#: Jobs that hold hundreds of megabytes while they run. On the shared
#: instance the API lives in the same container, so starting one of these
#: with the ceiling already close takes the whole studio down, not the job.
HEAVY = ("analyze", "full", "proxy", "rebuild", "render")

#: How much has to be free before a heavy job is claimed.
HEADROOM_BYTES = 700 * 1024 * 1024


def memory_free(root=None) -> int | None:
    """Bytes still available to this container, or None where it cannot be
    known (a Mac, a machine without cgroups): there the guard stands aside."""
    import pathlib as _pathlib

    root = _pathlib.Path(root) if root else _pathlib.Path("/sys/fs/cgroup")
    try:
        limit_text = (root / "memory.max").read_text().strip()
        used = int((root / "memory.current").read_text().strip())
    except (OSError, ValueError):
        try:  # cgroup v1
            limit_text = (root / "memory/memory.limit_in_bytes").read_text().strip()
            used = int((root / "memory/memory.usage_in_bytes").read_text().strip())
        except (OSError, ValueError):
            return None
    if limit_text == "max":
        return None
    try:
        limit = int(limit_text)
    except ValueError:
        return None
    if limit <= 0 or limit > 1 << 50:  # no real limit set
        return None
    return max(0, limit - used)


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
        self.current: str | None = None  # the id of the job in flight, if any
        self.handlers: dict[str, Callable[[Session, RenderJob, Project], dict]] = {
            "analyze": self.handle_analyze,
            "full": self.handle_full,
            "rebuild": self.handle_rebuild,
            "validate": self.handle_validate,
            "sync": self.handle_sync,
            "render": self.handle_render,
            "proxy": self.handle_proxy,
            "transcribe": self.handle_unsupported,
        }

    def short_of_memory(self) -> bool:
        """Whether a heavy job would start too close to the ceiling."""
        if self.kinds is not None and not any(kind in HEAVY for kind in self.kinds):
            return False
        free = memory_free()
        return free is not None and free < HEADROOM_BYTES

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
        if self.short_of_memory():
            logger.warning(
                "memoria quasi finita (%s liberi): aspetto prima di prendere un altro lavoro",
                _megabytes(memory_free()),
            )
            time.sleep(self.settings.worker_poll_seconds * 5)
            return False
        with self.session_factory() as session:
            job = job_service.claim(session, self.name, kinds=self.kinds)
            session.commit()
            if job is None:
                return False
            job_id, kind = job.id, job.kind

        self.current = job_id
        try:
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
        finally:
            self.current = None
        return True

    def hand_back(self) -> str | None:
        """Return the job in flight to the queue, because this worker is being
        stopped and will not finish it. Returns the job id, or None when the
        worker was idle."""
        job_id = self.current
        if job_id is None:
            return None
        with self.session_factory() as session:
            job = session.get(RenderJob, job_id)
            if job is not None and job.status == JOB_RUNNING and job.locked_by == self.name:
                job_service.release(session, job, f"il worker {self.name} è stato fermato a metà")
                session.commit()
        self.current = None
        return job_id

    def reap_orphans(self) -> int:
        """Requeue what a previous worker of this host left running."""
        with self.session_factory() as session:
            count = job_service.reap_orphans(
                session, self.name, sole=getattr(self.settings, "worker_sole", True)
            )
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
        # The editor needs a picture and a waveform to correct the cut on: a
        # separate, low-priority job, queued once, so the proposal is not
        # kept waiting on a second encode.
        proxy_job = None
        if not self.storage.exists(asset_service.object_key(project, "proxy/index.json")):
            proxy_job = job_service.enqueue(
                session, project, kind="proxy",
                payload={"source": str(job.payload.get("source") or "")}, priority=-1,
            ).id
        return {
            "words": len(analysis.words),
            "duration": analysis.duration,
            "provider": analysis.provider,
            "proposal": plan.stats(),
            "proxyJob": proxy_job,
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

    def _raw_in_workspace(self, session: Session, project: Project, job: RenderJob) -> None:
        """Put the raw take where `build_source` reads it. An upload lands in
        storage; the proxy job reads it from there, but the rebuild read only
        the workspace and failed with «raw non disponibile» on every film that
        came in through the browser."""
        import shutil

        relative = job.payload.get("source") or self._timeline_source(session, project)
        target = self.workspace.project_dir(project.slug) / relative
        if target.is_file():
            return
        try:
            found = self._source_for(session, project, job)
        except NotSupported:
            return  # nowhere at all: `build_source` says so in its own words
        target.parent.mkdir(parents=True, exist_ok=True)
        if found.resolve() != target.resolve():
            shutil.move(str(found), target)

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
        if cuts.has_manual(session, project) and not job.payload.get("discardManual"):
            raise StudioError(
                "questo montaggio è stato corretto a mano: rifarlo da capo lo perderebbe. "
                "Per applicare e rigenerare le correzioni c'è il job 'rebuild'; "
                "per ripartire dal girato, 'discardManual': true"
            )
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
        self._raw_in_workspace(session, project, job)
        self.workspace.build_source(project.slug)
        self.workspace.sync(project.slug)
        problems = self.workspace.validate(project.slug)
        if problems:
            raise StudioError("progetto non valido dopo il montaggio:\n- " + "\n- ".join(problems))
        if job.payload.get("render", True):
            report["render"] = self._render_if_possible(session, job, project)
        return report

    def handle_rebuild(self, session: Session, job: RenderJob, project: Project) -> dict:
        """Everything downstream of an applied timeline, again.

        A person corrected the cut and applied it: the documents are already
        written. What is left is the slow half of `full` — picture and sound
        rebuilt, the composition synced and checked, the film rendered — so
        that saving a correction is the whole job, not the first of four.
        """
        self._require_workspace(project)
        report: dict = {"timelineRevision": job.payload.get("timelineRevision")}
        self._raw_in_workspace(session, project, job)
        self.workspace.build_source(project.slug)
        self.workspace.sync(project.slug)
        problems = self.workspace.validate(project.slug)
        if problems:
            raise StudioError("progetto non valido dopo la correzione:\n- " + "\n- ".join(problems))
        if job.payload.get("render", True):
            report["render"] = self._render_if_possible(session, job, project)
        return report

    def _render_if_possible(self, session: Session, job: RenderJob, project: Project) -> dict:
        """The render, unless the media it plays are not on this machine.

        The hand-made film's AI angles, effects and music live on the Mac that
        made them, never on the server: rendering here would fail after the
        documents were already rebuilt, and a correction that was applied
        would read as a failed job. The job succeeds, and says where to render.
        """
        missing = self.workspace.missing_media(project.slug)
        if missing:
            return {"skipped": True, "missing": missing, "reason": _render_elsewhere(project.slug, missing)}
        return self.handle_render(session, job, project)

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
        missing = self.workspace.missing_media(project.slug)
        if missing:
            raise StudioError(_render_elsewhere(project.slug, missing))
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
        """Everything the timeline draws the footage with, in one job.

        The camera original is HEVC in a .mov: no browser will decode it, so
        without this the editor shows a black rectangle and the timeline is
        useless. The proxy is half resolution H.264 with short closed groups
        of pictures; beside it go the audio peaks and the thumbnail sheets,
        and a manifest that says where all of it is (`analysis.media`).
        """
        import json
        import pathlib
        import subprocess
        import tempfile

        from skyground.analysis import audio, media
        from skyground.core.workspace import find_ffmpeg
        from skyground.services import assets as asset_service

        ffmpeg = find_ffmpeg()
        source = self._source_for(session, project, job)
        name, media_type, encoder = PROXY_FORMATS.get(
            self.settings.proxy_codec, PROXY_FORMATS["h264"]
        )
        codec = "vp9" if name.endswith(".webm") else "h264"
        quiet = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        stored: dict[str, tuple] = {}

        def keep(relative: str, path: pathlib.Path, content_type: str, kind: str = "proxy"):
            key = asset_service.object_key(project, f"proxy/{relative}")
            item = self.storage.put_file(key, path, content_type)
            asset_service.register(
                session, project, key=key, kind=kind, size=item.size, sha256=item.sha256,
                content_type=content_type, meta={"job": job.id},
            )
            stored[relative] = (key, item)
            return key

        with tempfile.TemporaryDirectory(prefix="skyground-proxy-") as temporary:
            base = pathlib.Path(temporary)
            output = base / name
            subprocess.run(
                [*quiet, "-i", str(source), "-vf", f"scale=-2:960,fps={media.PROXY_FPS}",
                 *encoder, str(output)],
                check=True,
            )
            proxy_key = keep(name, output, media_type)

            # Peaks: mono 8 kHz PCM straight out of ffmpeg, folded as it streams.
            decoder = subprocess.Popen(
                [*quiet, "-i", str(source), "-vn", "-ac", "1",
                 "-ar", str(media.PEAK_SAMPLE_RATE), "-f", "s16le", "-"],
                stdout=subprocess.PIPE,
            )
            assert decoder.stdout is not None
            peaks = media.peaks_from_pcm(decoder.stdout)
            if decoder.wait() != 0:
                raise StudioError("ffmpeg non ha decodificato l'audio per la forma d'onda")
            peaks_path = base / "peaks.u8"
            peaks_path.write_bytes(peaks)
            peaks_key = keep("peaks.u8", peaks_path, "application/octet-stream")

            # Thumbnails: one a second, tiled into sheets.
            subprocess.run(
                [*quiet, "-i", str(source), "-vf", media.thumbnail_filter(), "-q:v", "5",
                 "-start_number", "0", str(base / "thumbs-%03d.jpg")],
                check=True,
            )
            thumb_keys = [
                keep(sheet.name, sheet, "image/jpeg")
                for sheet in sorted(base.glob("thumbs-*.jpg"))
            ]

            duration = audio.probe_duration(source)
            index = media.manifest(
                proxy_key=proxy_key, codec=codec, duration=duration,
                peaks_key=peaks_key, thumb_keys=thumb_keys,
                source_sha256=(job.payload or {}).get("sha256", ""),
            )
            index_path = base / "index.json"
            index_path.write_text(json.dumps(index), encoding="utf-8")
            manifest_key = keep("index.json", index_path, "application/json")

        return {
            "key": proxy_key,
            "size": stored[name][1].size,
            "manifest": manifest_key,
            "peaks": len(peaks),
            "thumbs": len(thumb_keys),
        }

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
            # A deploy is replacing the container, and the platform kills it a
            # few seconds after this signal: there is no "after the current
            # job". The job goes back to the queue now, so the worker that
            # comes up in the new container starts it over at once, and this
            # process ends without waiting for ffmpeg or the model to return.
            self.running = False
            handed = self.hand_back()
            if handed:
                logger.info("arresto richiesto, job %s restituito alla coda", handed)
                os._exit(0)
            logger.info("arresto richiesto, chiudo")

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


def _render_elsewhere(slug: str, missing: list[str]) -> str:
    return (
        f"il render non parte qui: mancano {', '.join(missing)}. "
        f"Si rende sul Mac che ha i media: python3 studio.py render-remote {slug}"
    )


def _megabytes(value: int | None) -> str:
    return "?" if value is None else f"{value // (1024 * 1024)} MB"
