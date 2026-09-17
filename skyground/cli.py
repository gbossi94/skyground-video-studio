"""Command line interface.

The editorial commands (`list`, `status`, `validate`, `pull`, `sync`,
`build-source`, `render`) keep their names, their arguments and their output and
work with the standard library alone. The cloud commands (`serve`, `db`, `users`,
`projects`, `worker`) import the application lazily, so a checkout without
`requirements.txt` installed still edits and renders the video.
"""

from __future__ import annotations

import argparse
import pathlib
import getpass
import os
import sys

from skyground.core.workspace import Workspace, default_workspace
from skyground.errors import ValidationError, StudioError


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

    monta = subcommands.add_parser(
        "monta", help="da un video grezzo a un progetto montato e renderizzato, da solo"
    )
    monta.add_argument("project")
    monta.add_argument("--video", required=True, help="il girato grezzo")
    monta.add_argument("--name", default=None, help="nome del progetto")
    monta.add_argument("--template", default="beauty-centers-growth-01",
                       help="progetto da cui copiare brand, audio e musica")
    monta.add_argument("--no-render", action="store_true", help="fermati prima del render")

    evaluate = subcommands.add_parser(
        "evaluate", help="confronta la proposta del motore col montaggio approvato"
    )
    evaluate.add_argument("project")
    evaluate.add_argument(
        "--transcript",
        required=True,
        help="trascrizione del girato (JSON con words, silences, duration)",
    )
    evaluate.add_argument("--json", action="store_true", help="solo i numeri, per uno script")

    export = subcommands.add_parser(
        "export", help="scrive il montaggio per un editor: FCPXML (Resolve, Premiere, Final Cut) e SRT"
    )
    export.add_argument("project")
    export.add_argument("--format", choices=["fcpxml", "srt", "capcut", "all"], default="all")
    export.add_argument("--capcut-root", default=None,
                        help="cartella delle bozze di CapCut sul Mac che aprirà la bozza (default SKYGROUND_CAPCUT_DRAFTS)")
    export.add_argument("--capcut-sample", default=None,
                        help="cartella di una bozza salvata da CapCut, da cui prendere la forma (default <workspace>/capcut/sample)")

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
    users.add_argument("action", choices=["create", "ensure", "list", "password", "deactivate"])
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


def run_evaluate(args, workspace: Workspace) -> int:
    """Score the engine against the edit a person approved.

    The reference is `timeline.json`: the clips that were actually kept. Without
    a number the engine can drift for a whole day while everybody believes it is
    roughly right.
    """
    import json

    from skyground.analysis import evaluation, pipeline
    from skyground.analysis.adviser import build_adviser
    from skyground.analysis.models import Analysis, Silence, Word

    def as_silence(span) -> Silence:
        if isinstance(span, dict):
            return Silence(float(span["start"]), float(span["end"]))
        return Silence(float(span[0]), float(span[1]))

    raw = json.loads(pathlib.Path(args.transcript).read_text(encoding="utf-8"))
    analysis = Analysis(
        source=raw.get("source", "assets/raw.mov"),
        duration=float(raw["duration"]),
        words=[Word.from_dict(word) for word in raw["words"]],
        silences=[as_silence(span) for span in raw.get("silences", [])],
    )
    timeline = workspace.read_document(args.project, "timeline.json")
    reference = [(clip["start"], clip["end"]) for clip in timeline["clips"]]

    # Through `propose`, not `plan_cut`: the adviser is part of what production
    # runs, so a number that leaves it out measures a different engine.
    adviser = build_adviser()
    from skyground.analysis.editor import build_model
    from skyground.config import get_settings

    model = build_model() if get_settings().cut_engine == "editor" else None
    plan = pipeline.propose(analysis, adviser=adviser, model=model)
    score = evaluation.compare([(s.start, s.end) for s in plan.segments], reference)

    if args.json:
        print(json.dumps(score.as_dict(), indent=2))
        return 0

    print(score.summary())
    decided = sum(1 for q in plan.questions if q.answered_by == "motore")
    print(
        f"domande aperte: {sum(1 for q in plan.questions if not q.resolved)}"
        f" · decise dal motore: {decided} · consulente: {adviser.name}"
        f" · montatore: {plan.editor.get('model') or 'euristico'}"
    )
    print(f"\nTENUTO DAL MOTORE, SCARTATO DALL'EDITOR ({len(score.extra)} pezzi):")
    for line in evaluation.describe(score.extra, analysis.words):
        print("  " + line)
    if score.missing:
        print(f"\nTENUTO DALL'EDITOR, SCARTATO DAL MOTORE ({len(score.missing)} pezzi):")
        for line in evaluation.describe(score.missing, analysis.words):
            print("  " + line)
    return 0


def run_monta(args, workspace: Workspace) -> int:
    """The whole chain on the checkout, without a database: for a machine
    with ffmpeg, a key, and a video."""
    import json

    from skyground.analysis import align, editor, invariants, pipeline
    from skyground.analysis.editor import build_model
    from skyground.analysis.transcription import build_transcriber
    from skyground.config import get_settings
    from skyground.core import retime

    settings = get_settings()
    created = workspace.create_project(
        args.project, args.name or args.project, args.video, template_project=args.template
    )
    print(f"progetto {args.project}: {created['width']}×{created['height']}, {created['duration']:.1f}s")

    base = workspace.project_dir(args.project)
    analysis = pipeline.analyze(
        base / created["source"], transcriber=build_transcriber(settings), source_label=created["source"]
    )
    print(f"ascoltato: {len(analysis.words)} parole, {len(analysis.silences)} silenzi")
    (base / "analysis.json").write_text(json.dumps(analysis.as_dict(), ensure_ascii=False), encoding="utf-8")

    model = build_model(settings) if settings.cut_engine == "editor" else None
    plan = pipeline.propose(analysis, model=model)
    if plan.open_questions:
        raise ValidationError(f"{len(plan.open_questions)} domande aperte: il motore è impostato per chiedere")
    print(f"montato da {plan.editor.get('model') or 'euristico'}: {len(plan.segments)} segmenti, "
          f"{plan.stats()['outputDuration']:.1f}s, {sum(1 for q in plan.questions if q.id.startswith('edit:'))} tagli")
    for line in plan.editor.get("reviews", []):
        print("  rilettura:", line[:160])
    (base / "cutplan.json").write_text(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    timeline = workspace.read_document(args.project, "timeline.json")
    old_clips = list(timeline.get("clips", []))
    updated = pipeline.apply_to_timeline(plan, analysis, timeline)
    manifest = workspace.read_document(args.project, "project.json")
    fps = int(manifest["canvas"].get("fps", 30))
    updated["duration"] = retime.snap_to_frames(updated["clips"], fps)
    workspace.write_document(args.project, "timeline.json", updated)
    manifest["canvas"]["duration"] = updated["duration"]
    workspace.write_document(args.project, "project.json", manifest)
    words = align.prepare(analysis).words
    workspace.write_document(args.project, "captions.json", retime.captions_from(words, updated["clips"]))
    cards, lost = retime.move_cards(workspace.read_document(args.project, "cards.json"), old_clips, updated["clips"])
    workspace.write_document(args.project, "cards.json", cards)
    for line in lost:
        print("  card:", line)

    workspace.build_source(args.project)
    workspace.sync(args.project)
    problems = workspace.validate(args.project)
    if problems:
        raise ValidationError("progetto non valido dopo il montaggio:\n- " + "\n- ".join(problems))
    print("sorgente, colonna sonora, sottotitoli: pronti e validi")
    if args.no_render:
        return 0
    output = workspace.render(args.project)
    print(f"render: {output}")
    return 0


def run_editorial(args, workspace: Workspace) -> int | None:
    if args.command == "monta":
        return run_monta(args, workspace)
    if args.command == "evaluate":
        return run_evaluate(args, workspace)
    if args.command == "export":
        import os
        import pathlib

        kinds = ("fcpxml", "srt") if args.format == "all" else (args.format,)
        for kind in kinds:
            print(workspace.export(
                args.project, kind,
                drafts_root=args.capcut_root or os.environ.get("SKYGROUND_CAPCUT_DRAFTS", ""),
                sample=pathlib.Path(args.capcut_sample) if args.capcut_sample else None,
            ))
        return 0
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
        if args.action == "ensure":
            # Run at boot. Silent and successful when nothing is configured, so
            # it can sit in an entrypoint without a deployment having to opt in.
            from skyground.config import get_settings

            settings = get_settings()
            if not settings.admin_email or not settings.admin_password:
                return 0
            user, created = accounts.ensure_admin(
                session, settings.admin_email, settings.admin_password
            )
            # Never the password, and never a hint of it: this goes to the logs.
            print(
                f"amministratore {'creato' if created else 'già presente'}: {user.email}"
            )
            return 0
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
