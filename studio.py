#!/usr/bin/env python3
"""Skyground Video Studio: project CLI and local collaborative editor."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parent
PROJECTS = ROOT / "projects"
THEME = ROOT / "web"
EDITABLE_FILES = {
    "project.json",
    "timeline.json",
    "captions.json",
    "cards.json",
    "angles.json",
    "brand.json",
    "audio.json",
}


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def project_dir(project_id: str) -> pathlib.Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id):
        raise ValueError("project id non valido")
    path = (PROJECTS / project_id).resolve()
    if path.parent != PROJECTS.resolve() or not path.is_dir():
        raise FileNotFoundError(f"progetto non trovato: {project_id}")
    return path


def list_projects() -> list[dict]:
    projects = []
    for path in sorted(PROJECTS.glob("*/project.json")):
        project = read_json(path)
        project["path"] = str(path.parent.relative_to(ROOT))
        project["previewAvailable"] = (path.parent / project["files"]["preview"]).exists()
        projects.append(project)
    return projects


def validate_project(project_id: str) -> list[str]:
    base = project_dir(project_id)
    problems: list[str] = []
    project = read_json(base / "project.json")
    duration = float(project.get("canvas", {}).get("duration", 0))
    if project.get("schemaVersion") != 1:
        problems.append("project.json: schemaVersion deve essere 1")
    if duration <= 0:
        problems.append("project.json: durata non valida")

    for key, relative in project.get("files", {}).items():
        if key == "preview":
            continue
        if not (base / relative).exists():
            problems.append(f"file mancante: {relative}")

    timeline = read_json(base / "timeline.json")
    clips = timeline.get("clips", [])
    cursor = 0.0
    for index, clip in enumerate(clips):
        start = float(clip.get("start", -1))
        end = float(clip.get("end", -1))
        output_start = float(clip.get("output_start", -1))
        if start < 0 or end <= start:
            problems.append(f"timeline clip {index}: intervallo sorgente non valido")
        if abs(output_start - cursor) > 0.002:
            problems.append(
                f"timeline clip {index}: output_start {output_start:.3f}, atteso {cursor:.3f}"
            )
        cursor += max(0.0, end - start)
    if abs(cursor - duration) > 0.01:
        problems.append(f"timeline: durata {cursor:.3f}, progetto {duration:.3f}")

    cards = sorted(read_json(base / "cards.json"), key=lambda item: float(item["a"]))
    previous_end = -1.0
    for card in cards:
        start = float(card.get("a", -1))
        end = float(card.get("b", -1))
        if start < 0 or end <= start or end > duration + 0.002:
            problems.append(f"card {card.get('id')}: intervallo non valido")
        if start < previous_end - 0.002:
            problems.append(f"card {card.get('id')}: sovrapposta alla card precedente")
        previous_end = max(previous_end, end)

    captions = read_json(base / "captions.json")
    previous_time = -1.0
    for index, word in enumerate(captions):
        start = float(word.get("t", -1))
        end = float(word.get("end", -1))
        if start < previous_time - 0.002:
            problems.append(f"caption {index}: timestamp non ordinato")
        if start < 0 or end < start or end > duration + 0.25:
            problems.append(f"caption {index}: intervallo non valido")
        previous_time = start

    for angle in read_json(base / "angles.json"):
        if not angle.get("enabled"):
            continue
        start = float(angle.get("start", -1))
        length = float(angle.get("duration", -1))
        if start < 0 or length <= 0 or start + length > duration + 0.002:
            problems.append(f"angle {angle.get('id')}: intervallo non valido")
        asset = angle.get("asset")
        if not asset:
            problems.append(f"angle {angle.get('id')}: asset mancante")

    return problems


def asset_status(project_id: str) -> list[dict]:
    base = project_dir(project_id)
    lock = read_json(base / "assets.lock.json")
    status = []
    for item in lock["files"]:
        destination = base / item["destination"]
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


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_ffmpeg() -> str:
    configured = os.environ.get("FFMPEG_BIN")
    candidates = [
        configured,
        shutil.which("ffmpeg"),
        ROOT.parent / "work/video-env/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1",
    ]
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError("FFmpeg non disponibile; installalo o imposta FFMPEG_BIN")


def pull_assets(project_id: str) -> None:
    base = project_dir(project_id)
    lock = read_json(base / "assets.lock.json")
    if lock["provider"] != "github-release":
        raise RuntimeError(f"provider non supportato: {lock['provider']}")
    if shutil.which("gh") is None:
        raise RuntimeError("GitHub CLI (gh) non disponibile")

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
                raise RuntimeError(f"checksum non valido: {item['name']}")
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


def caption_groups(words: list[dict], cards: list[dict], duration: float) -> list[list[dict]]:
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


def sync_composition(project_id: str) -> None:
    base = project_dir(project_id)
    project = read_json(base / "project.json")
    duration = float(project["canvas"]["duration"])
    cards = read_json(base / "cards.json")
    captions = read_json(base / "captions.json")
    angles = read_json(base / "angles.json")
    composition = base / project["files"]["composition"]
    html = composition.read_text(encoding="utf-8")

    root_duration = re.search(r'data-composition-id="main"[^>]*data-duration="([^"]+)"', html)
    if not root_duration:
        raise RuntimeError("durata della composizione non trovata")
    old_duration = root_duration.group(1)
    html = html.replace(old_duration, str(duration))

    card_start = html.find('<div id="card-')
    caption_marker = '<div id="captions"></div>'
    card_end = html.find(caption_marker)
    if card_start < 0 or card_end < 0:
        raise RuntimeError("blocco card non trovato")
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
        f"const fullCards={cards_json};\nfullCards.forEach",
        html,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise RuntimeError("array fullCards non trovato")

    groups_json = json.dumps(
        caption_groups(captions, cards, duration), ensure_ascii=False, separators=(",", ":")
    )
    html, count = re.subn(
        r"const captionGroups=.*?;\ncaptionGroups\.forEach",
        f"const captionGroups={groups_json};\ncaptionGroups.forEach",
        html,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise RuntimeError("array captionGroups non trovato")

    angle_markup = "".join(
        f'<video id="angle-{angle["id"]}" class="clip" src="{pathlib.Path(angle["asset"]).name}" '
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
        raise RuntimeError("blocco angles non trovato")

    composition.write_text(html, encoding="utf-8")
    write_json(composition.parent / "cards.json", cards)
    write_json(composition.parent / "captions.json", captions)
    print(f"sincronizzato {project_id}: {len(cards)} cards, {len(captions)} parole")


def build_source(project_id: str) -> None:
    base = project_dir(project_id)
    project = read_json(base / "project.json")
    timeline_path = base / "timeline.json"
    timeline = read_json(timeline_path)
    raw = base / timeline["source"]
    if not raw.exists():
        raise FileNotFoundError(f"raw non disponibile: {raw}; esegui pull")
    ffmpeg = find_ffmpeg()

    filters = []
    inputs = []
    cursor = 0.0
    for index, clip in enumerate(timeline["clips"]):
        start = float(clip["start"])
        end = float(clip["end"])
        clip["output_start"] = cursor
        cursor += end - start
        filters.extend(
            [
                f"[0:v]trim=start={start}:end={end},setpts=N/(30*TB),fps=30[v{index}]",
                f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]",
            ]
        )
        inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(inputs) + f"concat=n={len(inputs)}:v=1:a=1[vout][aout]")
    output = base / "composition" / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "warning",
            "-i",
            str(raw),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    timeline["duration"] = cursor
    project["canvas"]["duration"] = cursor
    write_json(timeline_path, timeline)
    write_json(base / "project.json", project)
    print(f"source ricostruita: {cursor:.3f}s")


def render_project(project_id: str) -> None:
    base = project_dir(project_id)
    problems = validate_project(project_id)
    if problems:
        raise RuntimeError("progetto non valido:\n- " + "\n- ".join(problems))
    sync_composition(project_id)
    composition = base / "composition"
    output = base / "renders" / "latest.mp4"
    local_bin = os.environ.get("HYPERFRAMES_BIN") or shutil.which("hyperframes")
    command = [local_bin, "render"] if local_bin else ["npx", "--no-install", "hyperframes", "render"]
    command += ["--quality", "high", "--output", str(output)]
    subprocess.run(command, cwd=composition, check=True)
    subprocess.run([find_ffmpeg(), "-v", "error", "-i", str(output), "-f", "null", "-"], check=True)
    print(output)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "SkygroundStudio/0.1"

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
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
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
        if route == "/api/projects":
            self.send_json(list_projects())
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/files/([^/]+)", route)
        if match:
            project_id, filename = match.groups()
            if filename not in EDITABLE_FILES:
                self.send_json({"error": "file non modificabile"}, 403)
                return
            try:
                self.send_json(read_json(project_dir(project_id) / filename))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, 404)
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/status", route)
        if match:
            project_id = match.group(1)
            self.send_json(
                {"assets": asset_status(project_id), "problems": validate_project(project_id)}
            )
            return
        match = re.fullmatch(r"/media/([^/]+)/(.*)", route)
        if match:
            project_id, relative = match.groups()
            base = project_dir(project_id)
            target = (base / relative).resolve()
            if base.resolve() not in target.parents or not target.is_file():
                self.send_json({"error": "media non trovato"}, 404)
                return
            self.send_file(target)
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
            write_json(project_dir(project_id) / filename, value)
            problems = validate_project(project_id)
            self.send_json({"saved": True, "problems": problems})
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)

    def log_message(self, format_string, *args):
        print(format_string % args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Skyground Video Studio")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("list")
    for name in ["status", "validate", "pull", "sync", "build-source", "render"]:
        command = subcommands.add_parser(name)
        if name == "validate":
            command.add_argument("project", nargs="?")
        else:
            command.add_argument("project")
    serve = subcommands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()

    if args.command == "list":
        for project in list_projects():
            print(f"{project['id']:<32} {project['status']:<12} {project['name']}")
        return 0
    if args.command == "validate" and args.project is None:
        failures = 0
        for project in list_projects():
            problems = validate_project(project["id"])
            print(f"{project['id']}: {'OK' if not problems else 'ERRORE'}")
            for problem in problems:
                print(f"  - {problem}")
            failures += bool(problems)
        return int(bool(failures))
    if args.command == "status":
        for item in asset_status(args.project):
            print(f"{'OK' if item['present'] else '--'}  {item['destination']}")
        return 0
    if args.command == "validate":
        problems = validate_project(args.project)
        if problems:
            print("\n".join(f"- {problem}" for problem in problems))
            return 1
        print(f"{args.project}: OK")
        return 0
    if args.command == "pull":
        pull_assets(args.project)
    elif args.command == "sync":
        sync_composition(args.project)
    elif args.command == "build-source":
        build_source(args.project)
    elif args.command == "render":
        render_project(args.project)
    elif args.command == "serve":
        server = ThreadingHTTPServer((args.host, args.port), StudioHandler)
        print(f"Skyground Video Studio: http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"errore: {error}", file=sys.stderr)
        raise SystemExit(1)
