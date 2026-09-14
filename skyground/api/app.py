"""Application factory.

`create_app()` builds the whole service: configuration is validated before the
first request, the database engine and the storage backend are attached to the
application state, and the static panel is served from `web/`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from skyground import __version__
from skyground.config import AUTH_OPEN, Settings, get_settings
from skyground.core.workspace import REPOSITORY_ROOT, Workspace
from skyground.db import build_engine, create_all
from skyground.errors import StudioError
from skyground.services import accounts
from skyground.services import projects as project_service
from skyground.storage import build_storage

logger = logging.getLogger("skyground")

WEB_ROOT = REPOSITORY_ROOT / "web"

#: Methods that change state and therefore must come from our own origin.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def create_app(settings: Settings | None = None, *, engine=None) -> FastAPI:
    settings = settings or get_settings()
    settings.check_deployable()

    workspace = Workspace(settings.workspace_root)
    engine = engine or build_engine(settings)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if settings.uses_sqlite:
            # A local checkout should just work; production schemas are owned by
            # Alembic and never created implicitly.
            create_all(engine)
        if settings.auth_mode == AUTH_OPEN:
            with session_factory() as session:
                bootstrap_local_workspace(session, workspace)
                session.commit()
        yield
        engine.dispose()

    app = FastAPI(
        title="Skyground Video Studio",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.workspace = workspace
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage = build_storage(settings)

    register_error_handlers(app)
    register_security(app, settings)
    register_routes(app)
    return app


def bootstrap_local_workspace(session: Session, workspace: Workspace) -> None:
    """In local mode, every project in the checkout belongs to the local user."""
    user = accounts.ensure_local_user(session)
    project_service.sync_workspace(session, workspace, owner=user)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StudioError)
    async def studio_error(_request: Request, error: StudioError):
        from skyground.errors import Conflict

        # `{"error": "..."}` is the shape the first version returned and the
        # shape `web/app.js` reads.
        payload: dict = {"error": str(error)}
        if isinstance(error, Conflict) and error.current is not None:
            payload["current"] = error.current
            payload["etag"] = error.etag
        return JSONResponse(payload, status_code=error.status_code)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(_request: Request, error: FileNotFoundError):
        return JSONResponse({"error": str(error)}, status_code=404)


def register_security(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def same_origin_and_headers(request: Request, call_next):
        """Reject cross-site state changes and set the baseline headers.

        The session cookie is `SameSite=Lax`, which already keeps it off
        cross-site form posts; checking `Origin` closes the remaining gaps
        without needing a token in every form.
        """
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("Origin")
            if origin and not _origin_allowed(origin, request, settings):
                return JSONResponse({"error": "origine non consentita"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def _origin_allowed(origin: str, request: Request, settings: Settings) -> bool:
    if origin in settings.allowed_origins:
        return True
    host = request.headers.get("Host", "")
    return origin in {f"https://{host}", f"http://{host}"}


def register_routes(app: FastAPI) -> None:
    from skyground.api.routes import assets, auth, cut, jobs, media, projects

    @app.get("/api/health")
    @app.get("/healthz")
    def health(request: Request) -> dict:
        settings: Settings = request.app.state.settings
        return {
            "status": "ok",
            "version": __version__,
            "environment": settings.environment,
            "storage": request.app.state.storage.backend,
            "authMode": settings.auth_mode,
        }

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(assets.router)
    app.include_router(jobs.router)
    app.include_router(cut.router)
    app.include_router(media.router)

    if WEB_ROOT.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_ROOT / "index.html")

        @app.get("/app")
        @app.get("/app/")
        def editor():
            """The React editor. Built into `web/app`; absent in a checkout that
            has not run `npm run build`, which must not break the panel."""
            page = WEB_ROOT / "app" / "index.html"
            if not page.is_file():
                return JSONResponse(
                    {"error": "l'editor non è compilato: esegui `npm run build` in app/"},
                    status_code=503,
                )
            return FileResponse(page)

        @app.get("/{filename:path}")
        def panel(filename: str):
            """Serve the panel's own files and fall back to `index.html`."""
            candidate = (WEB_ROOT / filename).resolve()
            if WEB_ROOT in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            if filename.startswith(("api/", "media/")):
                return JSONResponse({"error": "not found"}, status_code=404)
            return FileResponse(WEB_ROOT / "index.html")


def build_default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    return create_app()
