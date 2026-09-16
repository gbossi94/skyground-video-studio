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


def caption_groups(words: list[dict], cards: list[dict], duration: float) -> list[list[dict]]:
    """Group caption words into on-screen lines, skipping motion-card windows.

    Skyground rule: a motion graphic and a caption never state the same thing at
    the same time, so every word inside a card interval is dropped.
    """
    blocked = [(0.0, 8.43)] + [(float(card["a"]), float(card["b"])) for card in cards]
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
        html = html.replace(old_duration, str(duration))

        card_start = html.find('<div id="card-')
        caption_marker = '<div id="captions"></div>'
        card_end = html.find(caption_marker)
        if card_start < 0 or card_end < 0:
            raise StudioError("blocco card non trovato")
        card_markup = "".join(
            f'<div id="card-{card["id"]}" class="newcard {card["kind"]}">'
            f'<div class="clabel">{card["label"]}</div>'
            f'<div class="cbody">{card["body"]}</div></div>'
            for card in cards
        )
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

        groups = caption_groups(captions, cards, duration)
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
            r'(<video id="raw".*?</video>)(?:<video id="angle-.*?</video>)+',
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
                     "-vf", f"fps={fps}", "-frames:v", str(frames),
                     "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-video_track_timescale", "30000",
                     # `apad` perché un pezzo che finisce dove finisce il girato
                     # avrebbe meno audio che video, e il silenzio è preferibile
                     # a uno scarto.
                     "-af", f"aresample=48000,apad",
                     "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                     "-t", f"{length:.6f}",
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
        return cursor

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
        local_bin = os.environ.get("HYPERFRAMES_BIN") or shutil.which("hyperframes")
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
