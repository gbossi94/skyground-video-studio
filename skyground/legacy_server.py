"""The original single-file server, kept for a checkout with no dependencies.

`studio.py serve` prefers the production application; when `requirements.txt` is
not installed it falls back to this server so that editing the video never
depends on the cloud work. The routes and payloads are identical, which is also
what makes it a useful reference when changing the new API.

It listens on the loopback interface, has no authentication and must never be
exposed: production runs `skyground.api`.
"""

from __future__ import annotations

import json
import mimetypes
import pathlib
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from skyground.config import LOCAL_USER_EMAIL, LOCAL_USER_NAME
from skyground.core.workspace import EDITABLE_FILES, Workspace, read_json, write_json
from skyground.errors import StudioError

THEME = pathlib.Path(__file__).resolve().parents[1] / "web"


def make_handler(workspace: Workspace):
    class StudioHandler(BaseHTTPRequestHandler):
        server_version = "SkygroundStudio/0.2-local"

        def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, value, status: int = 200) -> None:
            self.send_bytes(
                (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def send_file(self, path: pathlib.Path) -> None:
            size = path.stat().st_size
            start, end = 0, size - 1
            status = 200
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
                if not match:
                    self.send_error(416)
                    return
                if match.group(1):
                    start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
                if start > end or start >= size:
                    self.send_error(416)
                    return
                status = 206
            self.send_response(status)
            self.send_header(
                "Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            remaining = end - start + 1
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def do_GET(self):
            route = urllib.parse.urlparse(self.path).path
            if route in ("/api/health", "/healthz"):
                self.send_json({"status": "ok", "mode": "legacy"})
                return
            if route == "/api/auth/me":
                # The panel asks who it is talking to; here it is always the
                # single local user, the same identity the app uses in open mode.
                self.send_json(
                    {
                        "user": {
                            "email": LOCAL_USER_EMAIL,
                            "name": LOCAL_USER_NAME,
                            "role": "owner",
                        },
                        "authMode": "open",
                    }
                )
                return
            if route == "/api/projects":
                self.send_json(workspace.list_projects())
                return
            match = re.fullmatch(r"/api/projects/([^/]+)/files/([^/]+)", route)
            if match:
                project_id, filename = match.groups()
                if filename not in EDITABLE_FILES:
                    self.send_json({"error": "file non modificabile"}, 403)
                    return
                try:
                    self.send_json(read_json(workspace.project_dir(project_id) / filename))
                except (OSError, ValueError, StudioError) as error:
                    self.send_json({"error": str(error)}, 404)
                return
            match = re.fullmatch(r"/api/projects/([^/]+)/status", route)
            if match:
                project_id = match.group(1)
                try:
                    self.send_json(
                        {
                            "assets": workspace.asset_status(project_id),
                            "problems": workspace.validate(project_id),
                        }
                    )
                except (OSError, ValueError, StudioError) as error:
                    self.send_json({"error": str(error)}, 404)
                return
            match = re.fullmatch(r"/media/([^/]+)/(.*)", route)
            if match:
                project_id, relative = match.groups()
                try:
                    self.send_file(workspace.media_path(project_id, relative))
                except (OSError, StudioError):
                    self.send_json({"error": "media non trovato"}, 404)
                return
            relative = route.lstrip("/") or "index.html"
            target = (THEME / relative).resolve()
            if THEME.resolve() not in target.parents and target != THEME.resolve():
                self.send_json({"error": "not found"}, 404)
                return
            if not target.is_file():
                target = THEME / "index.html"
            self.send_file(target)

        def do_PUT(self):
            route = urllib.parse.urlparse(self.path).path
            match = re.fullmatch(r"/api/projects/([^/]+)/files/([^/]+)", route)
            if not match:
                self.send_json({"error": "not found"}, 404)
                return
            project_id, filename = match.groups()
            if filename not in EDITABLE_FILES:
                self.send_json({"error": "file non modificabile"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(length))
                write_json(workspace.project_dir(project_id) / filename, value)
                self.send_json({"saved": True, "problems": workspace.validate(project_id)})
            except (OSError, ValueError, StudioError) as error:
                self.send_json({"error": str(error)}, 400)

        def log_message(self, format_string, *args):
            print(format_string % args)

    return StudioHandler


def serve(workspace: Workspace, host: str = "127.0.0.1", port: int = 4173) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(workspace))
    print(f"Skyground Video Studio (locale): http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
