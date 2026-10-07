"""Skyground → DaVinci Resolve: the cut lands on a Resolve timeline in one click.

Put this file where Resolve looks for scripts and it appears under
Workspace → Scripts → Utility → Skyground:

    macOS    ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
    Windows  %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\

Next to it, or in the home folder, a `skyground.json` says where the studio is
and who you are:

    {"url": "https://skyground-studio.onrender.com",
     "email": "…", "password": "…",
     "project": ""}            ← empty: the project whose cut is newest

Which project: the one named on the command line (`python3 Skyground.py
test-260917`), else the one in `skyground.json`, else the project whose
latest successful cut is the most recent, else the only one there is.

On each run the script signs in, takes the project's cut (timeline, raw
footage, captions), keeps a copy under ~/Movies/Skyground/<project>/, and
builds in Resolve a project of the same name with a 1080×1920 30 fps timeline:
one clip per clip of the cut, frame-exact on the raw footage, plus the
captions as a subtitle track. Nothing is rendered: from here the hand
decides. Standard library only — Resolve runs it with the system Python 3.

Everything it says goes to Resolve's console and to
~/Movies/Skyground/skyground.log, traceback included, so a run can be read
after the fact from a terminal.

Two steps when Resolve has no Python. Resolve only lists and runs Python
scripts when it finds a Python it accepts, and on some Macs it finds none:
the menu shows Lua scripts only. For that case the same work is split in
two, one click each:

    python3 Skyground.py          ← in a terminal: signs in, downloads the
                                    cut, writes ~/Movies/Skyground/latest.lua
    Workspace → Scripts → Skyground    ← in Resolve: Skyground.lua, next to
                                    this file, builds project and timeline
                                    from that manifest with the same rules

Run from a terminal this script never touches Resolve; it fetches and
writes the manifest, then says which click comes next.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

FPS = 30
LOG = pathlib.Path.home() / "Movies" / "Skyground" / "skyground.log"


def say(message: str) -> None:
    """To the console and to the log file, so a run inside Resolve can be
    read from a terminal afterwards."""
    print(message)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


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

    def jobs(self, project: str) -> list[dict]:
        return self.get_json(f"/api/projects/{urllib.parse.quote(project)}/jobs")

    def document(self, project: str, name: str):
        payload = self.get_json(f"/api/projects/{urllib.parse.quote(project)}/files/{name}")
        return payload.get("content", payload) if isinstance(payload, dict) and "content" in payload else payload


def latest_cut(studio: Studio, project: dict) -> str:
    """When this project's cut last succeeded, as the API's ISO timestamp,
    or "" if it never did. The API lists jobs newest first."""
    try:
        jobs = studio.jobs(project["id"])
    except (urllib.error.URLError, ValueError, KeyError):
        return ""
    for job in jobs:
        if job.get("kind") == "full" and job.get("status") == "succeeded":
            return job.get("finishedAt") or job.get("createdAt") or ""
    return ""


def choose(studio: Studio, projects: list[dict], wanted: str = "") -> dict:
    """The project to bring over: the one asked for, or the one whose cut
    is newest, or the only one."""
    if wanted:
        for project in projects:
            if wanted in (project["id"], project.get("name")):
                return project
        raise SystemExit(f"Skyground: progetto «{wanted}» non trovato; disponibili: {', '.join(p['id'] for p in projects)}")
    if len(projects) == 1:
        return projects[0]
    dated = [(latest_cut(studio, project), project) for project in projects]
    with_cut = [pair for pair in dated if pair[0]]
    if with_cut:
        return max(with_cut, key=lambda pair: pair[0])[1]
    return projects[0]


def fetch(studio: Studio, project_id: str, folder: pathlib.Path) -> dict:
    """The cut and what it needs, on disk: returns paths and documents."""
    project = studio.document(project_id, "project.json")
    timeline = studio.document(project_id, "timeline.json")
    source = timeline["source"]
    raw = folder / pathlib.Path(source).name
    if not raw.exists() or raw.stat().st_size == 0:
        say(f"Skyground: scarico il girato {source}…")
        studio.download(f"/media/{urllib.parse.quote(project_id)}/{source}", raw)
    subtitles = folder / f"{project_id}.srt"
    studio.download(f"/api/projects/{urllib.parse.quote(project_id)}/export/srt", subtitles)
    fcpxml = folder / f"{project_id}.fcpxml"
    studio.download(f"/api/projects/{urllib.parse.quote(project_id)}/export/fcpxml", fcpxml)
    return {"project": project, "timeline": timeline, "raw": raw, "subtitles": subtitles, "fcpxml": fcpxml}


def lua_string(text: str) -> str:
    """A Lua string literal for any text, quotes and newlines included."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def manifest(fetched: dict, name: str, fps: int = FPS) -> str:
    """The cut as a Lua table, for Skyground.lua inside Resolve: what to
    load, and each clip as seconds into the raw footage plus the whole
    number of timeline frames the studio decided on."""
    clips = []
    for clip in fetched["timeline"]["clips"]:
        start = float(clip["start"])
        frames = int(clip.get("frames") or round((float(clip["end"]) - start) * fps))
        clips.append(f"    {{start = {start!r}, frames = {frames}}},")
    canvas = fetched["project"]["canvas"]
    lines = [
        "-- written by Skyground.py; read by Skyground.lua from Resolve's Scripts menu",
        "return {",
        f"  name = {lua_string(name)},",
        f"  width = {int(canvas['width'])}, height = {int(canvas['height'])}, fps = {fps},",
        f"  raw = {lua_string(str(fetched['raw']))},",
        f"  subtitles = {lua_string(str(fetched['subtitles']))},",
        f"  fcpxml = {lua_string(str(fetched['fcpxml']))},",
        "  clips = {",
        *clips,
        "  },",
        "}",
        "",
    ]
    return "\n".join(lines)


def write_manifest(fetched: dict, name: str, folder: pathlib.Path, fps: int = FPS) -> pathlib.Path:
    """`cut.lua` beside the footage, and `latest.lua` one level up that
    points at the same cut: the Lua side has no directory listing, only a
    path it knows."""
    text = manifest(fetched, name, fps)
    cut = folder / "cut.lua"
    cut.write_text(text, encoding="utf-8")
    (folder.parent / "latest.lua").write_text(text, encoding="utf-8")
    return cut


# ---------------------------------------------------------------- the frames


def clip_ranges(timeline: dict, clip_fps: float, fps: int = FPS) -> list[tuple[int, int]]:
    """Each clip of the cut as (startFrame, endFrame) *of the raw footage*, at
    the raw footage's own frame rate, which is what Resolve wants; the length
    is the whole number of timeline frames the studio already decided on.
    Resolve 21 reads endFrame as exclusive: a clip of 166 frames is
    (135, 301), and (135, 300) lands 165 frames on the timeline."""
    ranges = []
    for clip in timeline["clips"]:
        start = float(clip["start"])
        frames = int(clip.get("frames") or round((float(clip["end"]) - start) * fps))
        length = frames / fps
        first = int(round(start * clip_fps))
        ranges.append((first, first + max(1, int(round(length * clip_fps)))))
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
    # Before any media: the frame rate is locked once the pool has a clip.
    # (timelinePlaybackFrameRate is read-only and follows this one.)
    for key, value in (
        ("timelineFrameRate", str(fps)),
        ("timelineResolutionWidth", str(canvas["width"])),
        ("timelineResolutionHeight", str(canvas["height"])),
    ):
        if not project.SetSetting(key, value):
            say(f"Skyground: Resolve ha rifiutato {key} = {value}")

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
        say(f"Skyground: Resolve ha messo {len(appended)} clip su {len(infos)}")

    # The captions: the SRT goes into the pool as a Subtitle clip, and lands
    # on the timeline only once a subtitle track exists to receive it.
    subtitles = "no"
    try:
        srt_items = pool.ImportMedia([str(fetched["subtitles"])]) or []
        if srt_items and timeline.AddTrack("subtitle") and pool.AppendToTimeline(srt_items):
            subtitles = "sì"
    except Exception as error:  # noqa: BLE001 - Resolve's API raises plain exceptions
        say(f"Skyground: sottotitoli non importati ({error}); importali da File → Import → Subtitle")
    return {"project": name, "timeline": timeline.GetName(), "clips": len(appended), "clip_fps": clip_fps, "subtitles": subtitles}


# ---------------------------------------------------------------------- main


def settings() -> dict:
    here = pathlib.Path(__file__).resolve().parent
    for candidate in (here / "skyground.json", pathlib.Path.home() / "skyground.json", pathlib.Path.home() / ".skyground.json"):
        if candidate.exists():
            config = json.loads(candidate.read_text(encoding="utf-8"))
            missing = [key for key in ("url", "email", "password") if not config.get(key)]
            if missing:
                raise SystemExit(f"Skyground: in {candidate} manca {', '.join(missing)}")
            return config
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
    named = [arg for arg in argv if not arg.startswith("--")]
    chosen = choose(studio, projects, named[0] if named else (config.get("project") or ""))
    if len(projects) > 1:
        say("Skyground: progetti disponibili: " + ", ".join(p["id"] for p in projects)
            + " (per sceglierne uno: python3 Skyground.py <id>)")
    folder = pathlib.Path(config.get("folder") or (pathlib.Path.home() / "Movies" / "Skyground")) / chosen["id"]
    say(f"Skyground: progetto «{chosen['name']}» ({chosen['id']}) → {folder}")
    fetched = fetch(studio, chosen["id"], folder)
    say(f"Skyground: {len(fetched['timeline']['clips'])} clip, girato {fetched['raw'].name}, sottotitoli {fetched['subtitles'].name}")
    cut = write_manifest(fetched, chosen["name"], folder)
    if "--dry-run" in argv or resolve is None:
        say(f"Skyground: manifesto per Resolve in {cut}")
        say("Skyground: prova a secco, niente da fare in Resolve" if "--dry-run" in argv
            else "Skyground: ora in DaVinci Resolve: Workspace → Scripts → Skyground mette il montaggio in timeline")
        return 0
    result = build(resolve, chosen["name"], fetched)
    say(f"Skyground: timeline «{result['timeline']}» con {result['clips']} clip a {result['clip_fps']:g} fps; sottotitoli: {result['subtitles']}")
    return 0


def run(resolve=None, argv: list[str] | None = None) -> int:
    """`main`, with whatever goes wrong written out in full: Resolve's
    console only shows the last line of an exception."""
    try:
        return main(resolve, argv)
    except SystemExit as stop:
        if stop.code not in (None, 0):
            say(str(stop.code))
        return 1 if stop.code else 0
    except Exception:  # noqa: BLE001 - this is the last stop before Resolve
        say("Skyground: errore\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    # Run from Resolve's menu, `resolve` is already defined; from a shell it is not.
    sys.exit(run(globals().get("resolve")))
