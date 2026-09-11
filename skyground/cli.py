"""Command line interface.

The editorial commands (`list`, `status`, `validate`, `pull`, `sync`,
`build-source`, `render`) keep their names, their arguments and their output and
work with the standard library alone. The cloud commands (`serve`, `db`, `users`,
`projects`, `worker`) import the application lazily, so a checkout without
`requirements.txt` installed still edits and renders the video.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from skyground.core.workspace import Workspace, default_workspace
from skyground.errors import StudioError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studio.py", description="Skyground Video Studio")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("list", help="elenca i progetti")
    for name, help_text in [
        ("status", "stato degli asset"),
        ("pull", "scarica i media"),
        ("sync", "aggiorna la composizione"),
        ("build-source", "ricostruisce source.mp4 dalla timeline"),
        ("render", "renderizza il progetto"),
    ]:
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("project")
    validate = subcommands.add_parser("validate", help="valida uno o tutti i progetti")
    validate.add_argument("project", nargs="?")

    serve = subcommands.add_parser("serve", help="avvia il pannello")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="ricarica a ogni modifica")
    serve.add_argument(
        "--legacy",
        action="store_true",
        help="usa il server locale senza dipendenze",
    )

    worker = subcommands.add_parser("worker", help="esegue i job in coda")
    worker.add_argument("--once", action="store_true", help="esegue un solo job e termina")
    worker.add_argument("--kinds", default="", help="tipi di job separati da virgola")

    database = subcommands.add_parser("db", help="gestione dello schema")
    database.add_argument("action", choices=["upgrade", "downgrade", "current", "history"])
    database.add_argument("--revision", default="head")

    users = subcommands.add_parser("users", help="gestione degli account")
    users.add_argument("action", choices=["create", "list", "password", "deactivate"])
    users.add_argument("email", nargs="?")
    users.add_argument("--name", default="")
    users.add_argument("--admin", action="store_true")

    projects = subcommands.add_parser("projects", help="registro dei progetti")
    projects.add_argument(
        "action", choices=["register", "members", "grant", "revoke", "adopt", "export"]
    )
    projects.add_argument("project", nargs="?")
    projects.add_argument("--email", default="")
    projects.add_argument("--role", default="editor")
    return parser


# ------------------------------------------------------------------ editorial


def run_editorial(args, workspace: Workspace) -> int | None:
    if args.command == "list":
        for project in workspace.list_projects():
            print(f"{project['id']:<32} {project['status']:<12} {project['name']}")
        return 0
    if args.command == "validate":
        if args.project is None:
            failures = 0
            for project in workspace.list_projects():
                problems = workspace.validate(project["id"])
                print(f"{project['id']}: {'OK' if not problems else 'ERRORE'}")
                for problem in problems:
                    print(f"  - {problem}")
                failures += bool(problems)
            return int(bool(failures))
        problems = workspace.validate(args.project)
        if problems:
            print("\n".join(f"- {problem}" for problem in problems))
            return 1
        print(f"{args.project}: OK")
        return 0
    if args.command == "status":
        for item in workspace.asset_status(args.project):
            print(f"{'OK' if item['present'] else '--'}  {item['destination']}")
        return 0
    if args.command == "pull":
        workspace.pull_assets(args.project)
        return 0
    if args.command == "sync":
        result = workspace.sync(args.project)
        print(
            f"sincronizzato {args.project}: {result['cards']} cards, {result['words']} parole"
        )
        return 0
    if args.command == "build-source":
        duration = workspace.build_source(args.project)
        print(f"source ricostruita: {duration:.3f}s")
        return 0
    if args.command == "render":
        print(workspace.render(args.project))
        return 0
    return None


# ---------------------------------------------------------------------- cloud


def run_serve(args, workspace: Workspace) -> int:
    from skyground.config import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    if args.legacy:
        from skyground.legacy_server import serve

        serve(workspace, host, port)
        return 0
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        from skyground.legacy_server import serve

        print(
            "dipendenze non installate: avvio il server locale.\n"
            "Per l'applicazione completa esegui `pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        serve(workspace, host, port)
        return 0

    import uvicorn

    print(f"Skyground Video Studio: http://{host}:{port}")
    uvicorn.run(
        "skyground.asgi:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def run_worker(args) -> int:
    from skyground.worker import Worker

    kinds = tuple(item.strip() for item in args.kinds.split(",") if item.strip()) or None
    worker = Worker(kinds=kinds)
    if args.once:
        print("job eseguito" if worker.run_once() else "nessun job in coda")
        return 0
    worker.run_forever()
    return 0


def run_db(args) -> int:
    from alembic import command
    from alembic.config import Config

    from skyground.core.workspace import REPOSITORY_ROOT

    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    if args.action == "upgrade":
        command.upgrade(config, args.revision if args.revision != "head" else "head")
    elif args.action == "downgrade":
        command.downgrade(config, args.revision)
    elif args.action == "current":
        command.current(config, verbose=True)
    else:
        command.history(config, verbose=True)
    return 0


def run_users(args) -> int:
    from sqlalchemy import select

    from skyground.db import session_scope
    from skyground.db.models import User
    from skyground.services import accounts

    with session_scope() as session:
        if args.action == "list":
            for user in session.scalars(select(User).order_by(User.email)):
                flags = " ".join(
                    filter(
                        None,
                        ["admin" if user.is_admin else "", "" if user.is_active else "off"],
                    )
                )
                print(f"{user.email:<40} {flags}")
            return 0
        if not args.email:
            print("email obbligatoria", file=sys.stderr)
            return 2
        if args.action == "create":
            password = read_password()
            user = accounts.create_user(
                session, args.email, password, name=args.name, is_admin=args.admin
            )
            print(f"creato {user.email}")
            return 0
        if args.action == "password":
            user = accounts.get_user(session, args.email)
            if user is None:
                print(f"utente non trovato: {args.email}", file=sys.stderr)
                return 1
            accounts.set_password(session, user, read_password())
            accounts.revoke_all_sessions(session, user)
            print(f"password aggiornata per {user.email}")
            return 0
        user = accounts.get_user(session, args.email)
        if user is None:
            print(f"utente non trovato: {args.email}", file=sys.stderr)
            return 1
        user.is_active = False
        accounts.revoke_all_sessions(session, user)
        print(f"disattivato {user.email}")
        return 0


def read_password() -> str:
    """Never take a password from the command line: it would end up in history."""
    password = os.environ.get("SKYGROUND_NEW_PASSWORD") or getpass.getpass("Password: ")
    if not os.environ.get("SKYGROUND_NEW_PASSWORD"):
        confirmation = getpass.getpass("Conferma: ")
        if password != confirmation:
            raise StudioError("le password non coincidono")
    return password


def run_projects(args, workspace: Workspace) -> int:
    from skyground.db import session_scope
    from skyground.services import accounts
    from skyground.services import projects as project_service

    with session_scope() as session:
        if args.action == "register":
            owner = require_owner(session, args.email)
            registered = (
                [
                    project_service.register_workspace_project(
                        session, workspace, args.project, owner=owner
                    )
                ]
                if args.project
                else project_service.sync_workspace(session, workspace, owner=owner)
            )
            for project in registered:
                print(f"registrato {project.slug} ({project.storage_mode})")
            return 0

        project = project_service.get_project(session, args.project)
        if args.action == "members":
            for membership, user in accounts.members(session, project):
                print(f"{user.email:<40} {membership.role}")
            return 0
        if args.action == "grant":
            user = accounts.get_user(session, args.email)
            if user is None:
                print(f"utente non trovato: {args.email}", file=sys.stderr)
                return 1
            accounts.add_member(session, project, user, args.role)
            print(f"{user.email} è {args.role} su {project.slug}")
            return 0
        if args.action == "revoke":
            user = accounts.get_user(session, args.email)
            if user is None:
                print(f"utente non trovato: {args.email}", file=sys.stderr)
                return 1
            accounts.remove_member(session, project, user)
            print(f"{user.email} rimosso da {project.slug}")
            return 0
        if args.action == "adopt":
            project_service.convert_to_managed(session, project, workspace)
            print(f"{project.slug} ora è gestito dal database")
            return 0
        written = project_service.export_to_workspace(session, project, workspace)
        print(f"esportati {len(written)} documenti in projects/{project.slug}")
        return 0


def require_owner(session, email: str):
    from skyground.config import get_settings
    from skyground.services import accounts

    if email:
        user = accounts.get_user(session, email)
        if user is None:
            raise StudioError(f"utente non trovato: {email}")
        return user
    if get_settings().is_production:
        raise StudioError("indica --email dell'owner in produzione")
    return accounts.ensure_local_user(session)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = default_workspace()

    result = run_editorial(args, workspace)
    if result is not None:
        return result
    if args.command == "serve":
        return run_serve(args, workspace)
    if args.command == "worker":
        return run_worker(args)
    if args.command == "db":
        return run_db(args)
    if args.command == "users":
        return run_users(args)
    if args.command == "projects":
        return run_projects(args, workspace)
    return 2


def run() -> int:
    """Entry point with the error reporting the CLI has always had."""
    import subprocess

    try:
        return main()
    except (StudioError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"errore: {error}", file=sys.stderr)
        return 1
