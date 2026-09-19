"""Filesystem workspace: the repository layout `projects/<id>/*.json`.

The logic in this module was extracted verbatim from the original `studio.py`
so that the existing project keeps validating, syncing and rendering exactly as
before. The only structural change is that the projects root is injectable,
which lets tests run against a temporary copy and lets the server work on a
checkout mounted anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime

from skyground.core import retime
from skyground.errors import NotFound, StudioError, ValidationError

PROJECT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")

#: JSON documents a person or an agent may edit through CLI, UI or API.
DOCUMENT_FILES = (
    "project.json",
    "timeline.json",
    "captions.json",
    "cards.json",
    "angles.json",
    "brand.json",
    "audio.json",
)

#: Kept as a set for the membership checks the legacy server performs.
EDITABLE_FILES = set(DOCUMENT_FILES)

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, value) -> None:
    """Write JSON the way the repository stores it: UTF-8, indented, LF, final newline."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def serialize_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_digest(value) -> str:
    """Stable digest of a document's content, used as an optimistic-locking etag."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_ffmpeg() -> str:
    configured = os.environ.get("FFMPEG_BIN")
    candidates = [
        configured,
        shutil.which("ffmpeg"),
        REPOSITORY_ROOT.parent
        / "work/video-env/lib/python3.13/site-packages/imageio_ffmpeg/binaries"
        / "ffmpeg-macos-aarch64-v7.1",
    ]
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            return str(candidate)
    raise StudioError("FFmpeg non disponibile; installalo o imposta FFMPEG_BIN")


def audio_seconds(path: pathlib.Path, rate: int = 48000) -> float:
    """How much sound a file holds, by decoding and counting the samples."""
    out = subprocess.run(
        [find_ffmpeg(), "-v", "error", "-i", str(path), "-vn", "-f", "s16le",
         "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True, check=True,
    ).stdout
    return len(out) / 2 / rate


def measured_duration(path: pathlib.Path) -> float:
    """How long a file really is, asked of the file itself.

    A clip cut at 4.575s–9.494s does not come out 4.919s long: the encoder lands
    on frame boundaries, and each piece gains a frame or so. Over thirty-nine
    clips that drifted to 1.2 seconds, and since cards and captions are timed
    against these numbers, the end of the video would have been more than a
    second out of step with its own subtitles.
    """
    probe = shutil.which("ffprobe") or str(pathlib.Path(find_ffmpeg()).with_name("ffprobe"))
    result = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def stream_durations(path: pathlib.Path) -> tuple[float, float]:
    """How long the picture lasts and how long the sound lasts, separately."""
    probe = shutil.which("ffprobe") or str(pathlib.Path(find_ffmpeg()).with_name("ffprobe"))

    def of(stream: str) -> float:
        result = subprocess.run(
            [probe, "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        text = result.stdout.strip().splitlines()
        return float(text[0]) if text and text[0] not in ("", "N/A") else 0.0

    return of("v:0"), of("a:0")


#: Audio fades at the edges of every piece, in seconds. See `build_source`.
FADE_IN = 0.04
FADE_OUT = 0.06


def _refuse_drift(path: pathlib.Path, fps: int, *, allowance: float = 0.5) -> None:
    """Refuse a piece whose sound and picture are not the same length.

    This is the check that was missing, and its absence cost a whole render. The
    old code validated that the *total duration* added up — and it did, because
    the total duration is the video's. Meanwhile every piece came out with the
    picture rounded up to a whole frame and the sound not, some forty
    milliseconds each, and `concat` glues the two streams separately: by the
    third clip the voice was ahead of the lips, and by the end of the film more
    than a second. A number that adds up is not the same as a film that works.
    """
    video, audio = stream_durations(path)
    if not audio:
        return  # nothing to be out of step with
    drift = abs(video - audio)
    if drift > allowance / fps:
        raise ValidationError(
            f"{path.name}: video {video:.3f}s e audio {audio:.3f}s non durano uguale "
            f"({drift * 1000:.0f} ms di scarto); concatenandoli il labiale si perde"
        )


def _is_neutral_composition(html: str) -> bool:
    """Whether a composition is the studio's neutral template: picture,
    captions drawn from captions.json, a mark and a progress bar.

    The template declares it with `data-plain-render`; a project created
    before that attribute existed carries the same skeleton without it, so
    the skeleton counts too. A hand-made composition names its own graphics
    and has neither.
    """
    if 'data-plain-render="1"' in html:
        return True
    return all(token in html for token in ('data-captions-from=', 'id="captions"', 'id="card-nessuna"'))


def _ffpath(path: pathlib.Path) -> str:
    """A path inside an ffmpeg filter option: colons and quotes escaped."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, rest = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{rest:05.2f}"


def _ass(groups: list[list[dict]], width: int, height: int, duration: float) -> str:
    """The captions as the neutral composition would have shown them.

    Same grouping as `sync`, same place on the canvas (the template's `.cap`
    sits with its top at 1570 of 1920), white with a shadow so it reads on any
    footage. One line at a time; a line ends where the next one begins.
    """
    margin_bottom = max(20, height - 1570 - 75)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,Jakarta,63,&H00FFFFFF,&H00FFFFFF,&H80000000,&HA0000000,-1,0,0,0,100,100,0,0,1,2,3,2,60,60,{margin_bottom},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for index, group in enumerate(groups):
        start = max(0.0, float(group[0]["t"]) - 0.035)
        end = min(float(group[-1]["end"]) + 0.055, duration)
        if index + 1 < len(groups):
            end = min(end, float(groups[index + 1][0]["t"]) - 0.04)
        if end <= start:
            continue
        text = " ".join(str(word["s"]) for word in group).replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def caption_groups(
    words: list[dict], cards: list[dict], duration: float, *, captions_from: float = 0.0
) -> list[list[dict]]:
    """Group caption words into on-screen lines, skipping motion-card windows.

    Skyground rule: a motion graphic and a caption never state the same thing at
    the same time, so every word inside a card interval is dropped. A
    composition with a hand-made opening says where its captions may start
    (`data-captions-from` on `#main`); the neutral template starts at zero.
    """
    blocked = [(0.0, float(captions_from))] + [(float(card["a"]), float(card["b"])) for card in cards]
    groups: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        time = float(word["t"])
        if any(start <= time < end for start, end in blocked):
            if current:
                groups.append(current)
                current = []
            continue
        candidate = current + [word]
        too_long = len(" ".join(item["s"] for item in candidate)) > 27
        long_gap = bool(current) and time - float(current[-1]["end"]) > 0.4
        if current and (len(current) >= 3 or too_long or long_gap):
            groups.append(current)
            current = []
        current.append(word)
        if re.search(r"[.!?]$", str(word["s"])):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def validate_documents(documents: dict, *, missing_files: list[str] | None = None) -> list[str]:
    """Validate a project from its documents alone.

    Takes `project`, `timeline`, `cards`, `captions` and `angles` already parsed,
    so the same rules apply to a project stored as files in the repository and to
    one stored as rows in the database. `missing_files` carries the checks that
    only a filesystem can perform; they keep their original position in the list
    so existing output stays identical.
    """
    problems: list[str] = []
    project = documents.get("project") or {}
    duration = float(project.get("canvas", {}).get("duration", 0))
    if project.get("schemaVersion") != 1:
        problems.append("project.json: schemaVersion deve essere 1")
    if duration <= 0:
        problems.append("project.json: durata non valida")

    problems.extend(missing_files or [])

    timeline = documents.get("timeline") or {}
    # A clip cut at 4.575–9.494 does not encode to exactly 4.919 seconds: the
    # encoder lands on a frame boundary and the piece gains up to a frame. So
    # `output_start` is *measured* when the source is rebuilt, and checking it
    # against the sum of the nominal lengths has to allow for that drift —
    # roughly one frame per clip — or it condemns the only numbers that match
    # the file. Anything larger is somebody having edited the timeline by hand
    # and forgotten to renumber, which is what this check is for.
    # Checked step by step rather than against a running total: a clip cut at
    # 4.575–9.494 does not encode to exactly 4.919 seconds — the encoder lands
    # on a frame boundary — so each piece can gain a frame or two, and a
    # cumulative check either rejects the measured numbers or, once its
    # tolerance is widened enough to accept them, stops catching anything.
    # Each clip must simply start where the previous one ended.
    slack = 2.0 / 30.0
    timeline_clips = timeline.get("clips", [])
    previous_start = None
    previous_length = 0.0
    total = 0.0
    for index, clip in enumerate(timeline_clips):
        start = float(clip.get("start", -1))
        end = float(clip.get("end", -1))
        output_start = float(clip.get("output_start", -1))
        if start < 0 or end <= start:
            problems.append(f"timeline clip {index}: intervallo sorgente non valido")
        length = max(0.0, end - start)
        if previous_start is None:
            expected = 0.0
        else:
            expected = previous_start + previous_length
        if abs(output_start - expected) > slack:
            problems.append(
                f"timeline clip {index}: output_start {output_start:.3f}, atteso {expected:.3f}"
            )
        previous_start, previous_length = output_start, length
        total = output_start + length
    if abs(total - duration) > slack:
        problems.append(f"timeline: durata {total:.3f}, progetto {duration:.3f}")

    cards = sorted(documents.get("cards") or [], key=lambda item: float(item["a"]))
    previous_end = -1.0
    for card in cards:
        start = float(card.get("a", -1))
        end = float(card.get("b", -1))
        if start < 0 or end <= start or end > duration + 0.002:
            problems.append(f"card {card.get('id')}: intervallo non valido")
        if start < previous_end - 0.002:
            problems.append(f"card {card.get('id')}: sovrapposta alla card precedente")
        previous_end = max(previous_end, end)

    previous_time = -1.0
    for index, word in enumerate(documents.get("captions") or []):
        start = float(word.get("t", -1))
        end = float(word.get("end", -1))
        if start < previous_time - 0.002:
            problems.append(f"caption {index}: timestamp non ordinato")
        if start < 0 or end < start or end > duration + 0.25:
            problems.append(f"caption {index}: intervallo non valido")
        previous_time = start

    for angle in documents.get("angles") or []:
        if not angle.get("enabled"):
            continue
        start = float(angle.get("start", -1))
        length = float(angle.get("duration", -1))
        if start < 0 or length <= 0 or start + length > duration + 0.002:
            problems.append(f"angle {angle.get('id')}: intervallo non valido")
        if not angle.get("asset"):
            problems.append(f"angle {angle.get('id')}: asset mancante")

    return problems


class Workspace:
    """A checkout of the repository seen as a collection of video projects."""

    def __init__(self, root: pathlib.Path | str | None = None):
        self.root = pathlib.Path(root).resolve() if root else REPOSITORY_ROOT
        self.projects = self.root / "projects"

    # ------------------------------------------------------------------ paths

    def project_dir(self, project_id: str) -> pathlib.Path:
        if not PROJECT_ID_PATTERN.fullmatch(project_id or ""):
            raise ValidationError("project id non valido")
        path = (self.projects / project_id).resolve()
        if path.parent != self.projects.resolve() or not path.is_dir():
            raise NotFound(f"progetto non trovato: {project_id}")
        return path

    def document_path(self, project_id: str, name: str) -> pathlib.Path:
        if name not in EDITABLE_FILES:
            raise ValidationError(f"file non modificabile: {name}")
        return self.project_dir(project_id) / name

    def media_path(self, project_id: str, relative: str) -> pathlib.Path:
        """Resolve a media path inside a project, refusing anything that escapes it."""
        base = self.project_dir(project_id)
        target = (base / relative).resolve()
        if base not in target.parents or not target.is_file():
            raise NotFound("media non trovato")
        return target

    def exists(self, project_id: str) -> bool:
        try:
            self.project_dir(project_id)
        except (NotFound, ValidationError):
            return False
        return True

    # -------------------------------------------------------------- documents

    def list_projects(self) -> list[dict]:
        projects = []
        for path in sorted(self.projects.glob("*/project.json")):
            project = read_json(path)
            project["path"] = str(path.parent.relative_to(self.root))
            project["previewAvailable"] = (path.parent / project["files"]["preview"]).exists()
            projects.append(project)
        return projects

    def read_document(self, project_id: str, name: str):
        path = self.document_path(project_id, name)
        if not path.is_file():
            raise NotFound(f"documento non trovato: {name}")
        return read_json(path)

    def write_document(self, project_id: str, name: str, value) -> None:
        write_json(self.document_path(project_id, name), value)

    def document_etag(self, project_id: str, name: str) -> str:
        """Digest of the document as stored on disk.

        Files can also change out of band (a `git pull`, a direct edit by an
        agent), so the etag is derived from the content itself instead of from a
        revision counter held elsewhere.
        """
        return content_digest(self.read_document(project_id, name))

    # ------------------------------------------------------------- validation

    def validate(self, project_id: str) -> list[str]:
        base = self.project_dir(project_id)
        project = read_json(base / "project.json")
        missing = [
            f"file mancante: {relative}"
            for key, relative in project.get("files", {}).items()
            if key != "preview" and not (base / relative).exists()
        ]
        documents = {
            name.removesuffix(".json"): read_json(base / name)
            for name in (
                "project.json",
                "timeline.json",
                "cards.json",
                "captions.json",
                "angles.json",
            )
        }
        return validate_documents(documents, missing_files=missing)

    # ------------------------------------------------------------------ media

    def asset_status(self, project_id: str) -> list[dict]:
        base = self.project_dir(project_id)
        lock = read_json(base / "assets.lock.json")
        status = []
        for item in lock["files"]:
            check = base / item.get("check", item["destination"])
            present = check.exists()
            status.append(
                {
                    "name": item["name"],
                    "destination": item["destination"],
                    "present": present,
                    "size": check.stat().st_size if present and check.is_file() else None,
                }
            )
        return status

    def pull_assets(self, project_id: str) -> None:
        base = self.project_dir(project_id)
        lock = read_json(base / "assets.lock.json")
        if lock["provider"] != "github-release":
            raise StudioError(f"provider non supportato: {lock['provider']}")
        if shutil.which("gh") is None:
            raise StudioError("GitHub CLI (gh) non disponibile")

        with tempfile.TemporaryDirectory(prefix="skyground-assets-") as temp_name:
            temp = pathlib.Path(temp_name)
            for item in lock["files"]:
                destination = base / item["destination"]
                check = base / item.get("check", item["destination"])
                if check.exists():
                    print(f"presente  {item['destination']}")
                    continue
                subprocess.run(
                    [
                        "gh",
                        "release",
                        "download",
                        lock["tag"],
                        "--repo",
                        lock["repository"],
                        "--pattern",
                        item["name"],
                        "--dir",
                        str(temp),
                    ],
                    check=True,
                )
                downloaded = temp / item["name"]
                expected = item.get("sha256")
                if expected and expected != "PENDING" and sha256(downloaded) != expected:
                    raise StudioError(f"checksum non valido: {item['name']}")
                if downloaded.suffix.lower() == ".zip":
                    destination.mkdir(parents=True, exist_ok=True)
                    prefix = item.get("archivePrefix", "")
                    with zipfile.ZipFile(downloaded) as archive:
                        for member in archive.infolist():
                            if member.is_dir() or not member.filename.startswith(prefix):
                                continue
                            relative = member.filename[len(prefix) :]
                            if not relative or ".." in pathlib.PurePosixPath(relative).parts:
                                continue
                            target = destination / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, target.open("wb") as output:
                                shutil.copyfileobj(source, output)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(downloaded), str(destination))
                print(f"scaricato {item['destination']}")

    # ------------------------------------------------------------ composition

    def sync(self, project_id: str) -> dict:
        base = self.project_dir(project_id)
        project = read_json(base / "project.json")
        duration = float(project["canvas"]["duration"])
        cards = read_json(base / "cards.json")
        captions = read_json(base / "captions.json")
        angles = read_json(base / "angles.json")
        composition = base / project["files"]["composition"]
        html = composition.read_text(encoding="utf-8")

        root_duration = re.search(
            r'data-composition-id="main"[^>]*data-duration="([^"]+)"', html
        )
        if not root_duration:
            raise StudioError("durata della composizione non trovata")
        old_duration = root_duration.group(1)
        # Only where the number means the duration. A global replace of the
        # old value worked while it was «115.93333333333325» and unique; on a
        # fresh composition whose duration is «1» it would rewrite every 1 in
        # the file.
        html = re.sub(
            r'data-duration="' + re.escape(old_duration) + '"',
            f'data-duration="{duration}"', html,
        )
        html = re.sub(
            r"(scaleX:1,duration:)" + re.escape(old_duration) + r"(,ease:'none')",
            lambda m: f"{m.group(1)}{duration}{m.group(2)}", html,
        )

        card_start = html.find('<div id="card-')
        caption_marker = '<div id="captions"></div>'
        card_end = html.find(caption_marker)
        if card_start < 0 or card_end < 0:
            raise StudioError("blocco card non trovato")
        card_markup = "".join(
            f'<div id="card-{card["id"]}" class="newcard {card.get("kind", "")}">'
            f'<div class="clabel">{card["label"]}</div>'
            f'<div class="cbody">{card["body"]}</div></div>'
            for card in cards
        )
        if not cards:
            # The marker the next sync looks for has to survive a film with no
            # motion graphics at all.
            card_markup = '<div id="card-nessuna" class="newcard" style="display:none"></div>'
        html = html[:card_start] + card_markup + html[card_end:]

        cards_json = json.dumps(cards, ensure_ascii=False, separators=(",", ":"))
        html, count = re.subn(
            r"const fullCards=.*?;\nfullCards\.forEach",
            lambda _match: f"const fullCards={cards_json};\nfullCards.forEach",
            html,
            count=1,
            flags=re.DOTALL,
        )
        if count != 1:
            raise StudioError("array fullCards non trovato")

        from_match = re.search(r'data-captions-from="([^"]+)"', html)
        captions_from = float(from_match.group(1)) if from_match else 0.0
        groups = caption_groups(captions, cards, duration, captions_from=captions_from)
        groups_json = json.dumps(groups, ensure_ascii=False, separators=(",", ":"))
        html, count = re.subn(
            r"const captionGroups=.*?;\ncaptionGroups\.forEach",
            lambda _match: f"const captionGroups={groups_json};\ncaptionGroups.forEach",
            html,
            count=1,
            flags=re.DOTALL,
        )
        if count != 1:
            raise StudioError("array captionGroups non trovato")

        angle_markup = "".join(
            f'<video id="angle-{angle["id"]}" class="clip" '
            f'src="{pathlib.Path(angle["asset"]).name}" '
            f'data-start="{angle["start"]}" data-duration="{angle["duration"]}" '
            f'data-media-start="{angle["mediaStart"]}" data-track-index="{angle["track"]}" '
            'style="position:absolute;left:0;top:0" muted></video>'
            for angle in angles
            if angle.get("enabled") and angle.get("asset")
        )
        html, count = re.subn(
            r'(<video id="raw".*?</video>)(?:<video id="angle-.*?</video>)*',
            lambda match: match.group(1) + angle_markup,
            html,
            count=1,
            flags=re.DOTALL,
        )
        if count != 1:
            raise StudioError("blocco angles non trovato")

        composition.write_text(html, encoding="utf-8")
        write_json(composition.parent / "cards.json", cards)
        write_json(composition.parent / "captions.json", captions)
        return {"cards": len(cards), "words": len(captions), "captionGroups": len(groups)}

    def create_project(
        self,
        project_id: str,
        name: str,
        video: pathlib.Path | str,
        *,
        template_project: str | None = None,
        client: str = "Skyground",
        language: str = "it",
    ) -> dict:
        """A new project around a raw video, ready for the editor.

        Until now the studio could edit a project somebody had already laid out
        by hand, and could not start one. This lays out the minimum a cut needs:
        the manifest with the canvas measured from the file, an empty timeline
        that points at the footage, no cards, no inserts, and the *neutral*
        composition — picture, captions, brand mark, progress bar. Motion
        graphics are a design job per film and are not pretended here.

        Brand, audio settings and music come from `template_project` when one
        is named and exists in the workspace, so a new film sounds and looks
        like the studio's others.
        """
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValidationError(f"identificativo non valido: {project_id}")
        base = (self.projects / project_id).resolve()
        if base.parent != self.projects.resolve():
            raise ValidationError(f"identificativo non valido: {project_id}")
        if base.exists():
            raise ValidationError(f"il progetto esiste già: {project_id}")
        self.projects.mkdir(parents=True, exist_ok=True)
        video = pathlib.Path(video)
        if not video.is_file():
            raise NotFound(f"video non trovato: {video}")

        probe = self._probe(video)
        source_name = f"raw{video.suffix.lower() or '.mov'}"
        (base / "assets").mkdir(parents=True)
        (base / "renders").mkdir()
        shutil.copyfile(video, base / "assets" / source_name)

        # The neutral composition, with the studio's music bed in it: the bed
        # is the one piece of media a new film cannot do without, and the
        # hand-made project's copy lives in a release archive that production
        # never pulls. First production film came out with a bare voice.
        template = pathlib.Path(__file__).resolve().parents[1] / "templates" / "composition"
        composition = base / "composition"
        shutil.copytree(template, composition)

        brand = {"name": client, "colors": {"accent": "#5c0bfe", "paper": "#f4f5f0", "ink": "#141414"},
                 "fonts": {"sans": "sans.ttf", "serif": "serif.ttf"}, "rules": []}
        audio = {
            "master": {"targetLufs": -16, "truePeak": -1.8},
            "voice": {"asset": "composition/voice.m4a", "targetLufs": -16},
            "music": {"asset": "composition/music.mp3", "targetLufs": -32, "loop": True},
            "renderedMix": "composition/soundtrack.m4a",
            "ducking": {"threshold": 0.03, "ratio": 4, "attackMs": 15, "releaseMs": 280},
        }
        if template_project and self.exists(template_project):
            origin = self.project_dir(template_project)
            for name_, target in (("brand.json", brand), ("audio.json", audio)):
                if (origin / name_).exists():
                    target.clear()
                    target.update(read_json(origin / name_))
            for media in ("music.mp3",):
                if (origin / "composition" / media).exists():
                    shutil.copyfile(origin / "composition" / media, composition / media)

        write_json(base / "project.json", {
            "schemaVersion": 1,
            "id": project_id,
            "name": name,
            "client": client,
            "status": "draft",
            "language": language,
            "canvas": {"width": 1080, "height": 1920, "fps": 30, "duration": round(probe["duration"], 3)},
            "source": {"width": probe["width"], "height": probe["height"], "fps": probe["fps"]},
            "files": {
                "timeline": "timeline.json", "captions": "captions.json", "cards": "cards.json",
                "angles": "angles.json", "brand": "brand.json", "audio": "audio.json",
                "composition": "composition/index.html", "preview": "preview.mp4",
            },
            "assets": {"lockfile": "assets.lock.json", "directory": "assets"},
            "render": {"engine": "hyperframes", "compositionId": "main", "quality": "high",
                       "outputDirectory": "renders"},
        })
        write_json(base / "timeline.json", {
            "source": f"assets/{source_name}",
            "clips": [{"start": 0.0, "end": round(probe["duration"], 3), "output_start": 0.0,
                       "label": "girato intero, non ancora montato"}],
            "corrections": [],
            "duration": round(probe["duration"], 3),
            "generatedBy": "create_project",
        })
        write_json(base / "captions.json", [])
        write_json(base / "cards.json", [])
        write_json(base / "angles.json", [])
        write_json(base / "brand.json", brand)
        write_json(base / "audio.json", audio)
        write_json(base / "assets.lock.json", {"version": 1, "files": []})
        (base / "README.md").write_text(
            f"# {name}\n\nProgetto creato da `{video.name}` "
            f"({probe['width']}×{probe['height']}, {probe['fps']:g} fps, {probe['duration']:.1f}s). "
            "Il montaggio è del motore: ogni taglio è nella coda di revisione dell'editor con il suo motivo.\n",
            encoding="utf-8",
        )
        return {"id": project_id, "source": f"assets/{source_name}", **probe}

    @staticmethod
    def _probe(video: pathlib.Path) -> dict:
        probe = shutil.which("ffprobe") or str(pathlib.Path(find_ffmpeg()).with_name("ffprobe"))
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate:format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        num, _, den = str(stream.get("r_frame_rate", "30/1")).partition("/")
        fps = float(num) / float(den or 1) if num else 30.0
        return {
            "width": int(stream.get("width", 0)),
            "height": int(stream.get("height", 0)),
            "fps": round(fps, 3),
            "duration": float(data.get("format", {}).get("duration", 0.0)),
        }

    def build_source(self, project_id: str) -> float:
        base = self.project_dir(project_id)
        project = read_json(base / "project.json")
        timeline_path = base / "timeline.json"
        timeline = read_json(timeline_path)
        raw = base / timeline["source"]
        if not raw.exists():
            raise NotFound(f"raw non disponibile: {raw}; esegui pull")
        ffmpeg = find_ffmpeg()

        # Un pezzo alla volta, poi si concatena. Un unico `filter_complex` con
        # un trim per clip fa decodificare a FFmpeg lo stesso file una volta per
        # clip, in parallelo: su trentanove clip di 1080×1920 in HEVC il
        # processo viene ucciso dal sistema. Così la memoria non dipende da
        # quante clip ci sono.
        #
        # Ogni pezzo dura un numero intero di fotogrammi, e il suo audio dura
        # esattamente altrettanto. È l'unico modo per concatenare copiando:
        # `concat` incolla i due flussi separatamente, quindi ogni millisecondo
        # di differenza fra video e audio dentro un pezzo si somma a quello dei
        # pezzi prima. Tagliando alla vecchia maniera il video si arrotondava al
        # fotogramma e l'audio no — una quarantina di millisecondi a clip, più
        # di un secondo in fondo al video: dopo la prima frase il labiale non
        # tornava più.
        #
        # L'audio intermedio è PCM apposta. A 48 kHz un fotogramma sono 1600
        # campioni esatti, mentre un frame AAC ne dura 1024 e non si può tagliare
        # dove serve. L'AAC si fa una volta sola, alla fine, sul montato.
        #
        # E i pezzi sono `.mov` perché Matroska non scrive la durata delle
        # singole tracce: in un `.mkv` il controllo che video e audio durino
        # uguale non si può nemmeno fare.
        fps = int(project["canvas"].get("fps", 30))
        output = base / "composition" / "source.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        cursor = 0.0
        with tempfile.TemporaryDirectory(prefix="skyground-source-") as work:
            pieces = []
            # The same numbers the timeline already carries — computed once,
            # in one place, so the file and the documents cannot disagree.
            retime.snap_to_frames(timeline["clips"], fps)
            for index, clip in enumerate(timeline["clips"]):
                start = float(clip["start"])
                frames = int(clip["frames"])
                length = frames / fps
                piece = pathlib.Path(work) / f"{index:04d}.mov"
                subprocess.run(
                    [ffmpeg, "-y", "-v", "error",
                     "-ss", f"{start:.6f}", "-i", str(raw),
                     # `setpts` prima di `fps`: dopo un seek il primo fotogramma
                     # decodificato non cade a zero — su un girato a 29,97 fps
                     # cadeva a due centesimi — e il filtro contava da lì,
                     # producendo un fotogramma in meno. La guardia sotto lo ha
                     # preso in produzione, su un pezzo di 138 fotogrammi uscito
                     # con 137.
                     "-vf", f"setpts=PTS-STARTPTS,fps={fps}", "-frames:v", str(frames),
                     # `fast`, not `medium`: at CRF 18 the picture is the same
                     # to the eye and the encode is three times quicker. In
                     # production the pieces took eleven minutes on a
                     # throttled CPU, longer than the model took to edit.
                     "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-video_track_timescale", "30000",
                     # `apad` perché un pezzo che finisce dove finisce il girato
                     # avrebbe meno audio che video, e il silenzio è preferibile
                     # a uno scarto; `atrim` chiude l'audio alla lunghezza
                     # esatta senza toccare il video, come farebbe `-t`.
                     # Short fades at both ends of every piece: a cut lands in a
                     # pause, but a pause is not digital silence — room tone,
                     # a breath, a lip smack — and a hard edge on it is a click
                     # at every join. Forty milliseconds in and sixty out sit
                     # inside the lead-in and lead-out, and never touch a word.
                     "-af", f"aresample=48000,asetpts=PTS-STARTPTS,apad,atrim=end={length:.6f},"
                            f"afade=t=in:st=0:d={FADE_IN},afade=t=out:st={max(0.0, length - FADE_OUT):.6f}:d={FADE_OUT}",
                     "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                     str(piece)],
                    check=True,
                )
                _refuse_drift(piece, fps)
                cursor += length
                pieces.append(piece)

            listing = pathlib.Path(work) / "pezzi.txt"
            listing.write_text(
                "".join(f"file '{piece}'\n" for piece in pieces), encoding="utf-8"
            )
            subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
                 "-movflags", "+faststart", str(output)],
                check=True,
            )
        _refuse_drift(output, fps, allowance=1.0)
        timeline["duration"] = cursor
        project["canvas"]["duration"] = cursor
        write_json(timeline_path, timeline)
        write_json(base / "project.json", project)
        # Sound and picture are rebuilt together, always. Leaving this to a
        # second command that somebody has to remember is how the renderer ended
        # up laying the previous edit's voice over a new cut.
        for line in self.build_soundtrack(project_id):
            print(f"colonna sonora: {line}")
        return cursor

    def build_soundtrack(self, project_id: str) -> list[str]:
        """Rebuild the mix from the cut that exists now. Returns what was left out.

        This did not exist, and its absence is what a person actually heard. The
        studio could re-cut the picture and could not re-cut the sound: the
        renderer reads `composition/soundtrack.m4a`, and that file was the mix of
        the *approved* edit, 115.9 seconds of a different arrangement of the same
        footage. Laid over a 113.7 second cut it agreed for exactly one sentence
        — the opening line, which both edits happen to start with — and then the
        voice was talking about something else than the mouth.

        The voice is not a separate asset to keep in step: it *is* the sound of
        the rebuilt source, so it is taken from there and can never disagree with
        the picture again. Music loops and has no timing of its own. Anything
        that was baked against a previous edit is named in the return value
        rather than laid over the wrong moments.
        """
        base = self.project_dir(project_id)
        composition = base / "composition"
        source = composition / "source.mp4"
        if not source.exists():
            raise NotFound("source.mp4 non c'è: esegui build-source")
        settings = read_json(base / "audio.json")
        project = read_json(base / "project.json")
        fps = int(project["canvas"].get("fps", 30))
        length = measured_duration(source)
        ffmpeg = find_ffmpeg()

        voice_target = float(settings.get("voice", {}).get("targetLufs", -16))
        music_target = float(settings.get("music", {}).get("targetLufs", -32))
        master = settings.get("master", {})
        peak = float(master.get("truePeak", -1.8))
        ducking = settings.get("ducking", {})

        voice = composition / pathlib.Path(
            settings.get("voice", {}).get("asset", "composition/voice.m4a")
        ).name
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(source), "-vn",
             # `asetpts` after `loudnorm`: the filter delays its timestamps by
             # its own look-ahead — 85 ms on the TEST film — while the samples
             # come out in place, so `-t` cut the file 85 ms short (the muxer
             # wrote the nominal length anyway) and a seek into the tail landed
             # 85 ms off. Timestamps rebuilt from the sample count, both hold.
             "-af", f"aresample=48000,loudnorm=I={voice_target}:TP={peak}:LRA=11,asetpts=N/SR/TB,apad",
             "-t", f"{length:.6f}",
             "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2", str(voice)],
            check=True,
        )

        left_out: list[str] = []
        effects = settings.get("effects", {}).get("asset")
        if effects and (base / effects).exists():
            left_out.append(
                f"{pathlib.Path(effects).name}: è un nastro già mixato sul montaggio "
                "precedente, non una lista di effetti con i loro tempi, quindi non si "
                "può rimettere a tempo — resta fuori invece di cadere sui momenti sbagliati"
            )

        mix = composition / pathlib.Path(
            settings.get("renderedMix", "composition/soundtrack.m4a")
        ).name
        music = composition / pathlib.Path(
            settings.get("music", {}).get("asset", "composition/music.mp3")
        ).name
        limit = 10 ** (peak / 20)
        if music.exists():
            # `asplit` because the voice is needed twice — once to duck the
            # music with and once in the mix — and a filter output can only be
            # consumed once. And the labels are words: a label of `[v]` or `[a]`
            # is read by ffmpeg as a stream specifier, not as a name.
            chain = (
                f"[0:a]aresample=48000,loudnorm=I={voice_target}:TP={peak}:LRA=11,asetpts=N/SR/TB,apad,"
                f"asplit=2[voce][chiave];"
                f"[1:a]aresample=48000,loudnorm=I={music_target}:TP={peak}:LRA=11,asetpts=N/SR/TB[musica];"
                f"[musica][chiave]sidechaincompress="
                f"threshold={float(ducking.get('threshold', 0.03))}:"
                f"ratio={float(ducking.get('ratio', 4))}:"
                f"attack={float(ducking.get('attackMs', 15))}:"
                f"release={float(ducking.get('releaseMs', 280))}[abbassata];"
                f"[voce][abbassata]amix=inputs=2:duration=first:normalize=0[insieme];"
                f"[insieme]alimiter=limit={limit:.4f}[uscita]"
            )
            subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-i", str(source),
                 "-stream_loop", "-1", "-i", str(music),
                 "-filter_complex", chain, "-map", "[uscita]", "-t", f"{length:.6f}",
                 "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2", str(mix)],
                check=True,
            )
        else:
            left_out.append(f"{music.name}: non c'è, la colonna sonora è la sola voce")
            shutil.copyfile(voice, mix)

        # The mix is what the renderer lays over the picture: if it is not the
        # same length, everything after the first cut is a guess.
        for produced in (voice, mix):
            # Counted in samples, not read from the header: the header said
            # 44.700 s of a file that held 44.615 s of sound.
            heard = audio_seconds(produced)
            drift = abs(heard - length)
            if drift > 1.0 / fps:
                raise ValidationError(
                    f"{produced.name} dura {heard:.3f}s contro i "
                    f"{length:.3f}s dell'immagine ({drift * 1000:.0f} ms): "
                    "sopra il montaggio non starebbe a tempo"
                )
        return left_out

    # ----------------------------------------------------------------- render

    def render_output_path(self, project_id: str, version: str | None = None) -> pathlib.Path:
        """Path of a new render. Never reuses the name of an existing render."""
        base = self.project_dir(project_id)
        directory = base / read_json(base / "project.json").get("render", {}).get(
            "outputDirectory", "renders"
        )
        version = version or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        candidate = directory / f"{project_id}-{version}.mp4"
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = directory / f"{project_id}-{version}-{suffix}.mp4"
        return candidate

    def missing_media(self, project_id: str) -> list[str]:
        """Every picture and sound the composition plays that is not on this
        disk. The renderer does not stop at the first: it fails after lint,
        compile and probe, with six warnings in a log nobody reads. Asked
        before, it is one sentence naming the files."""
        base = self.project_dir(project_id)
        composition = base / read_json(base / "project.json")["files"]["composition"]
        html = composition.read_text(encoding="utf-8")
        missing = []
        for src in re.findall(r'<(?:video|audio)\b[^>]*?\ssrc="([^"]+)"', html):
            if "://" in src or src.startswith("data:"):
                continue
            if not (composition.parent / src).exists() and src not in missing:
                missing.append(src)
        return missing

    def is_plain(self, project_id: str) -> bool:
        """Whether the film is picture, captions and a mark — and nothing that
        needs a browser to draw.

        The neutral composition declares it (`data-plain-render` on `#main`),
        and it holds only while the project has no motion graphics and no
        inserts: the first card somebody adds sends the film back to the
        browser renderer, which is the only thing that can draw it.
        """
        base = self.project_dir(project_id)
        html = (base / read_json(base / "project.json")["files"]["composition"]).read_text(encoding="utf-8")
        if not _is_neutral_composition(html):
            return False
        if read_json(base / "cards.json"):
            return False
        return not any(angle.get("enabled") for angle in read_json(base / "angles.json"))

    def export_fcpxml(self, project_id: str) -> str:
        """The cut as FCPXML, for DaVinci Resolve, Premiere Pro and Final Cut Pro."""
        from skyground.core import export

        project = read_json(self.project_dir(project_id) / "project.json")
        return export.fcpxml(self.project_dir(project_id), project.get("name") or project_id, read_json=read_json)

    def export_srt(self, project_id: str) -> str:
        """The captions as SRT, in output time, as the film shows them."""
        from skyground.core import export

        return export.srt(self.project_dir(project_id), read_json=read_json, caption_groups=caption_groups)

    @property
    def capcut_sample(self) -> pathlib.Path:
        """The draft CapCut itself saved, from which ours take their shape.
        Workspace data, uploaded once, never in the repository."""
        return self.root / "capcut" / "sample"

    def export_capcut(
        self, project_id: str, target: pathlib.Path, *, drafts_root: str, sample: pathlib.Path | None = None
    ) -> pathlib.Path:
        """The cut as a CapCut draft folder, zipped, with the raw footage inside."""
        from skyground.core import capcut

        if not drafts_root:
            raise ValidationError(
                "serve la cartella delle bozze di CapCut sulla macchina che aprirà la bozza "
                "(SKYGROUND_CAPCUT_DRAFTS, o il parametro root)"
            )
        project = read_json(self.project_dir(project_id) / "project.json")
        return capcut.write(
            self.project_dir(project_id), project.get("name") or project_id, target,
            sample=capcut.Sample(sample or self.capcut_sample), drafts_root=drafts_root,
            read_json=read_json, caption_groups=caption_groups, ffmpeg=find_ffmpeg(),
        )

    def export(self, project_id: str, kind: str, *, drafts_root: str = "", sample: pathlib.Path | None = None) -> pathlib.Path:
        """Write one export into the project's `exports/` folder and return it."""
        if kind not in ("fcpxml", "srt", "capcut"):
            raise ValidationError(f"formato di export sconosciuto: {kind} (fcpxml, srt o capcut)")
        folder = self.project_dir(project_id) / "exports"
        folder.mkdir(exist_ok=True)
        if kind == "capcut":
            return self.export_capcut(project_id, folder / f"{project_id}-capcut.zip", drafts_root=drafts_root, sample=sample)
        target = folder / f"{project_id}.{kind}"
        target.write_text(
            self.export_fcpxml(project_id) if kind == "fcpxml" else self.export_srt(project_id),
            encoding="utf-8",
        )
        return target

    def render_plain(self, project_id: str, output: pathlib.Path) -> pathlib.Path:
        """The film without a browser: ffmpeg burns what the neutral
        composition would have drawn.

        In production the browser renderer captured 3,475 frames at 1080×1920
        one screenshot at a time, in two gigabytes, at a tenth of a frame per
        second — hours for two minutes of film. For a film that is the picture,
        the captions, a mark and a progress bar, that machinery draws nothing
        ffmpeg cannot draw in a couple of minutes, and the result is the same
        picture and the same sound, because both were already rendered by
        `build_source`.
        """
        base = self.project_dir(project_id)
        composition = base / "composition"
        project = read_json(base / "project.json")
        canvas = project["canvas"]
        width, height, fps = int(canvas["width"]), int(canvas["height"]), int(canvas.get("fps", 30))
        duration = float(canvas["duration"])
        brand = read_json(base / "brand.json") if (base / "brand.json").exists() else {}
        accent = str(brand.get("colors", {}).get("accent") or brand.get("colors", {}).get("primary") or "#5c0bfe")

        captions = read_json(base / "captions.json")
        groups = caption_groups(captions, [], duration)
        subtitles = composition / "captions.ass"
        subtitles.write_text(_ass(groups, width, height, duration), encoding="utf-8")

        font = composition / "sans.ttf"
        mark = str(brand.get("name") or "SKYGROUND").upper().replace("'", "")
        filters = [
            # The film is the canvas, whatever the footage is: cover it and
            # crop, as the template's `object-fit: cover` does. The test
            # footage is 270×480 and came out 270×480 before this line.
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
            f"subtitles='{_ffpath(subtitles)}':fontsdir='{_ffpath(composition)}'",
            # The mark, top left, as the template places it.
            f"drawtext=fontfile='{_ffpath(font)}':text='{mark}':x=64:y=65:fontsize=23:fontcolor=white"
            ":shadowcolor=black@0.5:shadowx=0:shadowy=2",
            # The progress bar along the bottom edge, growing with time.
            f"drawbox=x=0:y={height - 10}:w='{width}*t/{duration:.6f}':h=10:color={accent}:t=fill",
        ]
        ffmpeg = find_ffmpeg()
        subprocess.run(
            [ffmpeg, "-y", "-v", "error",
             "-i", str(composition / "source.mp4"), "-i", str(composition / "soundtrack.m4a"),
             "-map", "0:v:0", "-map", "1:a:0",
             "-vf", ",".join(filters), "-r", str(fps),
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
             "-t", f"{duration:.6f}", "-movflags", "+faststart", str(output)],
            check=True,
        )
        return output

    def render(self, project_id: str, output: pathlib.Path | None = None) -> pathlib.Path:
        base = self.project_dir(project_id)
        problems = self.validate(project_id)
        if problems:
            raise ValidationError("progetto non valido:\n- " + "\n- ".join(problems))
        self.sync(project_id)
        composition = base / "composition"
        fps = int(read_json(base / "project.json")["canvas"].get("fps", 30))
        # Refuse to spend ten minutes rendering a source whose sound and picture
        # have already come apart: it would come out the other end just as
        # broken, and the drift is invisible in every number the render reports.
        built = composition / "source.mp4"
        if built.exists():
            _refuse_drift(built, fps, allowance=1.0)
        output = pathlib.Path(output) if output else self.render_output_path(project_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.is_plain(project_id):
            self.render_plain(project_id, output)
            subprocess.run(
                [find_ffmpeg(), "-v", "error", "-i", str(output), "-f", "null", "-"], check=True
            )
            _refuse_drift(output, fps, allowance=1.0)
            shutil.copyfile(output, output.parent / "latest.mp4")
            return output
        # The renderer is a dependency of *this repository*, not of the project
        # being rendered: resolved from here, whatever the working directory.
        # `npx --no-install` run inside a project on another disk found no
        # node_modules and tried to download the package instead.
        installed = REPOSITORY_ROOT / "node_modules" / ".bin" / "hyperframes"
        local_bin = (
            os.environ.get("HYPERFRAMES_BIN")
            or (str(installed) if installed.is_file() else None)
            or shutil.which("hyperframes")
        )
        command = (
            [local_bin, "render"] if local_bin else ["npx", "--no-install", "hyperframes", "render"]
        )
        command += ["--quality", "high", "--output", str(output)]
        subprocess.run(command, cwd=composition, check=True)
        subprocess.run(
            [find_ffmpeg(), "-v", "error", "-i", str(output), "-f", "null", "-"], check=True
        )
        _refuse_drift(output, fps, allowance=1.0)
        # `latest.mp4` stays a convenience pointer; the versioned file above is
        # the artefact that is never overwritten.
        latest = output.parent / "latest.mp4"
        shutil.copyfile(output, latest)
        return output


_default_workspace: Workspace | None = None


def default_workspace() -> Workspace:
    """Workspace rooted at `SKYGROUND_WORKSPACE_ROOT`, or at the repository."""
    global _default_workspace
    if _default_workspace is None:
        root = os.environ.get("SKYGROUND_WORKSPACE_ROOT") or REPOSITORY_ROOT
        _default_workspace = Workspace(root)
    return _default_workspace


# Backwards-compatible module level helpers used by `studio.py` and by scripts
# written against the first version of the tool.

def project_dir(project_id: str) -> pathlib.Path:
    return default_workspace().project_dir(project_id)


def list_projects() -> list[dict]:
    return default_workspace().list_projects()


def validate_project(project_id: str) -> list[str]:
    return default_workspace().validate(project_id)


def asset_status(project_id: str) -> list[dict]:
    return default_workspace().asset_status(project_id)


def pull_assets(project_id: str) -> None:
    default_workspace().pull_assets(project_id)


def sync_composition(project_id: str) -> dict:
    return default_workspace().sync(project_id)


def build_source(project_id: str) -> float:
    return default_workspace().build_source(project_id)


def render_project(project_id: str, output: pathlib.Path | None = None) -> pathlib.Path:
    return default_workspace().render(project_id, output)
