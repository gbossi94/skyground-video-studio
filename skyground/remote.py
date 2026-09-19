"""Render a production project on this machine and publish the film back.

A film with motion graphics is drawn by the browser renderer, and on the
production instance — two gigabytes, one worker, a screenshot per frame — that
is hours for two minutes of film. A Mac does it in minutes. So the Mac renders
and production keeps the record: this fetches the project's documents from the
studio online, rebuilds picture and sound here against the media this checkout
already has (the raw take, the AI angles, the music and effects that never
reach the server), renders, checks the file, and uploads it as a new version.

Standard library only, like the rest of the editorial commands. The password
is asked on the terminal and never stored; the session lives for this command.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from skyground.core.workspace import DOCUMENT_FILES, REPOSITORY_ROOT, Workspace, find_ffmpeg
from skyground.errors import StudioError

DEFAULT_SERVER = "https://skyground-studio.onrender.com"

#: Rebuilt by `build_source` and `build_soundtrack` in the copy: never taken
#: from the checkout, where they belong to a different cut.
REBUILT = {"source.mp4", "soundtrack.m4a", "voice.m4a"}
#: Not needed to render, and large.
SKIPPED = {"preview.mp4", "renders"}
#: Inputs the render only reads: hard-linked into the copy instead of copied.
LINKED_SUFFIXES = {".mov", ".mp4", ".mp3", ".wav", ".m4a", ".ttf", ".otf"}


class Remote:
    """A signed-in session with the studio online."""

    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def _request(self, method: str, path: str, *, body=None, headers=None, timeout=60):
        request = urllib.request.Request(
            self.server + path, data=body, method=method, headers=headers or {}
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except (ValueError, AttributeError):
                pass
            raise StudioError(f"{method} {path}: {error.code} {detail}") from None
        except urllib.error.URLError as error:
            raise StudioError(f"{self.server} non raggiungibile: {error.reason}") from None

    def login(self, email: str, password: str) -> dict:
        body = json.dumps({"email": email, "password": password}).encode()
        result = self._request("POST", "/api/auth/login", body=body,
                               headers={"Content-Type": "application/json"})
        return result["user"]

    def document(self, project: str, name: str):
        return self._request("GET", f"/api/projects/{quote(project)}/files/{quote(name)}")

    def cut(self, project: str) -> dict:
        return self._request("GET", f"/api/projects/{quote(project)}/cut")

    def upload_render(self, project: str, path: pathlib.Path) -> dict:
        size = path.stat().st_size
        with path.open("rb") as body:
            return self._request(
                "PUT",
                f"/api/projects/{quote(project)}/assets/renders/{quote(path.name)}",
                body=body,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                    "X-Skyground-Kind": "render",
                },
                timeout=1800,
            )


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def stage(checkout: Workspace, project: str, root: pathlib.Path) -> Workspace:
    """A private copy of the project to render in, so the checkout's own
    documents and built files are never touched. Media are hard links: the
    raw take alone is half a gigabyte."""
    source = checkout.project_dir(project)
    target = root / "projects" / project
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if relative.parts[0] in SKIPPED or path.name in REBUILT or path.is_dir():
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in LINKED_SUFFIXES:
            try:
                os.link(path, destination)
                continue
            except OSError:
                pass  # another volume: copy instead
        shutil.copy2(path, destination)
    return Workspace(root)


def probe(path: pathlib.Path) -> dict:
    """What the film is, in the numbers the project's rules name."""
    ffprobe = str(pathlib.Path(find_ffmpeg()).with_name("ffprobe"))
    output = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate:format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(output)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = any(s["codec_type"] == "audio" for s in data["streams"])
    num, den = (int(part) for part in video["r_frame_rate"].split("/"))
    return {
        "width": video["width"], "height": video["height"],
        "fps": num / den, "audio": audio, "duration": float(data["format"]["duration"]),
    }


def render_remote(
    checkout: Workspace,
    project: str,
    *,
    server: str,
    email: str,
    password: str,
    upload: bool = True,
    say=print,
) -> dict:
    remote = Remote(server)
    user = remote.login(email, password)
    say(f"entrato come {user.get('email', email)} su {remote.server}")

    # The raw take here must be the one production cut: the timeline is
    # seconds into it, and another file would put every cut somewhere else.
    state = remote.cut(project)
    remote_length = (state.get("plan") or {}).get("sourceDuration") or (state.get("analysis") or {}).get("duration")

    keep = REPOSITORY_ROOT / ".skyground" / "remote-renders"
    keep.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{project}-", dir=REPOSITORY_ROOT / ".skyground") as temp:
        workspace = stage(checkout, project, pathlib.Path(temp))
        base = workspace.project_dir(project)
        for name in DOCUMENT_FILES:
            content = remote.document(project, name)
            (base / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        timeline = json.loads((base / "timeline.json").read_text(encoding="utf-8"))
        raw = base / timeline["source"]
        if not raw.exists():
            raise StudioError(f"il girato {timeline['source']} non c'è su questo Mac: esegui pull")
        if remote_length:
            local_length = probe_duration(raw)
            if abs(local_length - float(remote_length)) > 0.1:
                raise StudioError(
                    f"il girato qui dura {local_length:.2f}s, quello in produzione {float(remote_length):.2f}s: "
                    "non è lo stesso file"
                )
        say(f"documenti della produzione: {len(timeline['clips'])} clip, {timeline.get('duration', 0):.1f}s")

        duration = workspace.build_source(project)
        say(f"immagine e suono ricostruiti: {duration:.2f}s")
        workspace.sync(project)
        problems = workspace.validate(project)
        if problems:
            raise StudioError("progetto non valido:\n- " + "\n- ".join(problems))
        say("render in corso…")
        output = workspace.render(project)
        film = keep / output.name
        shutil.move(str(output), film)

    facts = probe(film)
    say(
        f"render: {film.name} · {facts['width']}×{facts['height']} · {facts['fps']:.2f} fps · "
        f"{facts['duration']:.2f}s · audio {'sì' if facts['audio'] else 'NO'}"
    )
    if (facts["width"], facts["height"]) != (1080, 1920) or round(facts["fps"]) != 30 or not facts["audio"]:
        raise StudioError(f"il film non rispetta le regole del progetto, non lo carico: {facts}")
    result = {"file": str(film), **facts}
    if upload:
        say("carico in produzione…")
        asset = remote.upload_render(project, film)
        result["asset"] = asset
        say(f"pubblicato: {asset.get('key', film.name)}")
    return result


def probe_duration(path: pathlib.Path) -> float:
    ffprobe = str(pathlib.Path(find_ffmpeg()).with_name("ffprobe"))
    output = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(output.strip())
