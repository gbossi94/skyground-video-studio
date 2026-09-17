"""Skyground → DaVinci Resolve: the cut lands on a Resolve timeline in one click.

Put this file where Resolve looks for scripts and it appears under
Workspace → Scripts → Utility → Skyground:

    macOS    ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
    Windows  %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\

Next to it, or in the home folder, a `skyground.json` says where the studio is
and who you are:

    {"url": "https://skyground-studio.onrender.com",
     "email": "…", "password": "…",
     "project": ""}            ← empty: the most recent project

On each run the script signs in, takes the project's cut (timeline, raw
footage, captions), keeps a copy under ~/Movies/Skyground/<project>/, and
builds in Resolve a project of the same name with a 1080×1920 30 fps timeline:
one clip per clip of the cut, frame-exact on the raw footage, plus the
captions as a subtitle track. Nothing is rendered: from here the hand
decides. Standard library only — Resolve runs it with the system Python 3.

It can also run outside Resolve, for a dry run that only downloads:
    python3 Skyground.py --dry-run
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

FPS = 30


# ------------------------------------------------------------------ the studio


class Studio:
    """Signed-in access to the studio's API, with only the standard library."""

    def __init__(self, url: str, email: str, password: str):
        self.url = url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._post("/api/auth/login", {"email": email, "password": password})

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.opener.open(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_json(self, path: str):
        with self.opener.open(self.url + path, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def download(self, path: str, target: pathlib.Path) -> pathlib.Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.opener.open(self.url + path, timeout=3600) as response, open(target, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        return target

    def projects(self) -> list[dict]:
        return self.get_json("/api/projects")

    def document(self, project: str, name: str):
        payload = self.get_json(f"/api/projects/{urllib.parse.quote(project)}/files/{name}")
        return payload.get("content", payload) if isinstance(payload, dict) and "content" in payload else payload


def fetch(studio: Studio, project_id: str, folder: pathlib.Path) -> dict:
    """The cut and what it needs, on disk: returns paths and documents."""
    project = studio.document(project_id, "project.json")
    timeline = studio.document(project_id, "timeline.json")
    source = timeline["source"]
    raw = folder / pathlib.Path(source).name
    if not raw.exists() or raw.stat().st_size == 0:
        print(f"Skyground: scarico il girato {source}…")
        studio.download(f"/media/{urllib.parse.quote(project_id)}/{source}", raw)
    subtitles = folder / f"{project_id}.srt"
    studio.download(f"/api/projects/{urllib.parse.quote(project_id)}/export/srt", subtitles)
    fcpxml = folder / f"{project_id}.fcpxml"
    studio.download(f"/api/projects/{urllib.parse.quote(project_id)}/export/fcpxml", fcpxml)
    return {"project": project, "timeline": timeline, "raw": raw, "subtitles": subtitles, "fcpxml": fcpxml}


# ---------------------------------------------------------------- the frames


def clip_ranges(timeline: dict, clip_fps: float, fps: int = FPS) -> list[tuple[int, int]]:
    """Each clip of the cut as (startFrame, endFrame) *of the raw footage*, at
    the raw footage's own frame rate, which is what Resolve wants; the length
    is the whole number of timeline frames the studio already decided on."""
    ranges = []
    for clip in timeline["clips"]:
        start = float(clip["start"])
        frames = int(clip.get("frames") or round((float(clip["end"]) - start) * fps))
        length = frames / fps
        first = int(round(start * clip_fps))
        last = first + max(1, int(round(length * clip_fps))) - 1
        ranges.append((first, last))
    return ranges


# ---------------------------------------------------------------- in Resolve


def build(resolve, name: str, fetched: dict, fps: int = FPS) -> dict:
    """The Resolve project and timeline. `resolve` is the scripting object
    Resolve hands to a script run from its menu."""
    manager = resolve.GetProjectManager()
    project = manager.LoadProject(name) or manager.CreateProject(name)
    if project is None:
        raise RuntimeError(f"Resolve non ha creato né aperto il progetto «{name}»")
    canvas = fetched["project"]["canvas"]
    for key, value in (
        ("timelineResolutionWidth", str(canvas["width"])),
        ("timelineResolutionHeight", str(canvas["height"])),
        ("timelineFrameRate", str(fps)),
        ("timelinePlaybackFrameRate", str(fps)),
    ):
        project.SetSetting(key, value)

    pool = project.GetMediaPool()
    storage = resolve.GetMediaStorage()
    items = storage.AddItemListToMediaPool([str(fetched["raw"])]) or []
    if not items:
        raise RuntimeError(f"Resolve non ha importato il girato {fetched['raw']}")
    raw_item = items[0]
    try:
        clip_fps = float(raw_item.GetClipProperty("FPS") or fps)
    except (TypeError, ValueError):
        clip_fps = float(fps)

    title = "Skyground"
    timeline = pool.CreateEmptyTimeline(title)
    if timeline is None:  # exists already: a numbered one, never over the old
        number = 2
        while timeline is None:
            timeline = pool.CreateEmptyTimeline(f"{title} {number}")
            number += 1
    infos = [{"mediaPoolItem": raw_item, "startFrame": first, "endFrame": last}
             for first, last in clip_ranges(fetched["timeline"], clip_fps, fps)]
    appended = pool.AppendToTimeline(infos) or []
    if len(appended) != len(infos):
        print(f"Skyground: Resolve ha messo {len(appended)} clip su {len(infos)}")

    subtitles = "no"
    try:
        srt_items = pool.ImportMedia([str(fetched["subtitles"])]) or []
        if srt_items and pool.AppendToTimeline(srt_items):
            subtitles = "sì"
    except Exception as error:  # noqa: BLE001 - Resolve's API raises plain exceptions
        print(f"Skyground: sottotitoli non importati ({error}); importali da File → Import → Subtitle")
    return {"project": name, "timeline": timeline.GetName(), "clips": len(appended), "clip_fps": clip_fps, "subtitles": subtitles}


# ---------------------------------------------------------------------- main


def settings() -> dict:
    here = pathlib.Path(__file__).resolve().parent
    for candidate in (here / "skyground.json", pathlib.Path.home() / "skyground.json", pathlib.Path.home() / ".skyground.json"):
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise SystemExit(
        "Skyground: manca skyground.json (accanto allo script o nella home) con url, email, password"
    )


def main(resolve=None, argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config = settings()
    studio = Studio(config["url"], config["email"], config["password"])
    projects = studio.projects()
    if not projects:
        raise SystemExit("Skyground: nessun progetto visibile con questo account")
    wanted = config.get("project") or ""
    chosen = next((p for p in projects if p["id"] == wanted), None) if wanted else projects[0]
    if chosen is None:
        raise SystemExit(f"Skyground: progetto «{wanted}» non trovato; disponibili: {', '.join(p['id'] for p in projects)}")
    folder = pathlib.Path(config.get("folder") or (pathlib.Path.home() / "Movies" / "Skyground")) / chosen["id"]
    print(f"Skyground: progetto «{chosen['name']}» ({chosen['id']}) → {folder}")
    fetched = fetch(studio, chosen["id"], folder)
    print(f"Skyground: {len(fetched['timeline']['clips'])} clip, girato {fetched['raw'].name}, sottotitoli {fetched['subtitles'].name}")
    if "--dry-run" in argv or resolve is None:
        print("Skyground: prova a secco, niente da fare in Resolve" if "--dry-run" in argv
              else "Skyground: Resolve non c'è; lanciami dal menu Workspace → Scripts di DaVinci")
        return 0
    result = build(resolve, chosen["name"], fetched)
    print(f"Skyground: timeline «{result['timeline']}» con {result['clips']} clip a {result['clip_fps']:g} fps; sottotitoli: {result['subtitles']}")
    return 0


if __name__ == "__main__":
    # Run from Resolve's menu, `resolve` is already defined; from a shell it is not.
    sys.exit(main(globals().get("resolve")))
