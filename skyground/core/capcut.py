"""A CapCut desktop draft, written from the cut.

CapCut has no API for plugins and imports no timeline format. What it has is
a folder per project under its drafts root, with `draft_info.json` — tracks,
segments, materials, in microseconds — and `draft_meta_info.json`, the list
of imported media. Both are undocumented and change with the version, so this
never invents them: it takes a *sample* draft saved by the very CapCut that
will open the result, and reuses its shapes — the empty timeline CapCut keeps
in `template.tmp`, one video segment, one video material and its six little
companions, one text segment and one text material — filling in only what
the cut decides: where each clip starts in the raw footage, how long it
lasts, where it lands, and what the captions say and when.

The sample is data, not code: it lives in the workspace (`capcut/sample/`),
uploaded once by the person whose CapCut it is, and never in the repository.
"""

from __future__ import annotations

import copy
import io
import json
import pathlib
import shutil
import subprocess
import time
import uuid
import zipfile

from skyground.core import retime
from skyground.errors import NotFound, ValidationError

US = 1_000_000  # CapCut counts in microseconds

SAMPLE_FILES = ("draft_info.json", "draft_meta_info.json", "template.tmp")


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _read(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


class Sample:
    """The shapes taken from a draft CapCut itself saved."""

    def __init__(self, folder: pathlib.Path):
        self.folder = pathlib.Path(folder)
        info_path = self.folder / "draft_info.json"
        meta_path = self.folder / "draft_meta_info.json"
        if not info_path.exists() or not meta_path.exists():
            raise NotFound(
                "manca la bozza campione di CapCut: servono draft_info.json e draft_meta_info.json "
                f"in {self.folder}"
            )
        info = _read(info_path)
        self.meta = _read(meta_path)
        # The shape of a *real* draft, emptied — not CapCut's `template.tmp`,
        # which is the empty timeline with its values unset (colour space −1,
        # a 0×0 canvas, an old version) and made the first draft unopenable.
        self.skeleton = self._emptied(info)
        template = self.folder / "template.tmp"
        self.empty_timeline = _read(template) if template.exists() else self.skeleton
        self.platform = info.get("platform") or self.skeleton.get("platform") or {}
        self.config = info.get("config") or self.skeleton.get("config") or {}
        self.version = info.get("version", self.skeleton.get("version"))
        self.new_version = info.get("new_version", self.skeleton.get("new_version"))

        by_id: dict[str, tuple[str, dict]] = {}
        for kind, items in (info.get("materials") or {}).items():
            for item in items or []:
                if isinstance(item, dict) and "id" in item:
                    by_id[item["id"]] = (kind, item)
        tracks = info.get("tracks") or []
        video = next((t for t in tracks if t.get("type") == "video" and t.get("segments")), None)
        if video is None:
            raise ValidationError("la bozza campione non ha una traccia video con almeno una clip")
        self.video_track = {k: v for k, v in video.items() if k != "segments"}
        self.video_segment = copy.deepcopy(video["segments"][0])
        kind, material = by_id[self.video_segment["material_id"]]
        self.video_material = copy.deepcopy(material)
        self.video_extras = {
            by_id[ref][0]: copy.deepcopy(by_id[ref][1])
            for ref in self.video_segment.get("extra_material_refs", [])
            if ref in by_id
        }
        text = next((t for t in tracks if t.get("type") == "text" and t.get("segments")), None)
        if text is not None:
            self.text_track = {k: v for k, v in text.items() if k != "segments"}
            self.text_segment = copy.deepcopy(text["segments"][0])
        else:
            self.text_track = {**self.video_track, "type": "text", "flag": 1}
            self.text_segment = copy.deepcopy(self.video_segment)
            self.text_segment.update({"source_timerange": None, "hdr_settings": None, "enable_lut": False,
                                      "enable_adjust": False, "volume": 1.0, "last_nonzero_volume": 1.0})
        texts = (info.get("materials") or {}).get("texts") or []
        self.text_material = copy.deepcopy(texts[0]) if texts else None
        self.text_segment["extra_material_refs"] = []
        entries = [v for t in self.meta.get("draft_materials", []) if t.get("type") == 0 for v in t.get("value", [])]
        self.meta_material = copy.deepcopy(next((v for v in entries if v.get("metetype") == "video"), entries[0] if entries else {}))

    @staticmethod
    def _emptied(info: dict) -> dict:
        """The sample with everything that was *its* content taken out."""
        skeleton = copy.deepcopy(info)
        skeleton["tracks"] = []
        skeleton["materials"] = {k: [] for k in (info.get("materials") or {})}
        if isinstance(skeleton.get("keyframes"), dict):
            skeleton["keyframes"] = {k: [] for k in skeleton["keyframes"]}
        for key in ("relationships", "keyframe_graph_list", "lyrics_effects"):
            if isinstance(skeleton.get(key), list):
                skeleton[key] = []
        extra = skeleton.get("extra_info")
        if isinstance(extra, dict):
            skeleton["extra_info"] = {k: ([] if isinstance(v, list) else v) for k, v in extra.items()}
        config = skeleton.get("config")
        if isinstance(config, dict):
            for key in ("subtitle_taskinfo", "lyrics_taskinfo", "attachment_info"):
                if isinstance(config.get(key), list):
                    config[key] = []
            for key in ("subtitle_recognition_id", "lyrics_recognition_id"):
                if key in config:
                    config[key] = ""
        skeleton["group_container"] = None
        skeleton["time_marks"] = None
        return skeleton

    def companions(self) -> dict[str, bytes]:
        """Every file of the sample folder that is not the draft itself, the
        cover or media, by relative path: CapCut's own attachments, its binary
        `draft.extra`, copied as they are. Nothing in them names the content."""
        skip = {"draft_info.json", "draft_info.json.bak", "template.tmp", "template-2.tmp", "draft_meta_info.json",
                "draft_settings", "draft_cover.jpg", "key_value.json", "timeline_layout.json", "draft_biz_config.json",
                "draft_virtual_store.json", "draft_agency_config.json", "performance_opt_info.json", ".locked",
                "attachment_id_mapping.json", "patch.json", "mini_draft.json"}
        media = {".mov", ".mp4", ".m4a", ".mp3", ".wav", ".png", ".jpeg", ".jpg", ".zip"}
        found: dict[str, bytes] = {}
        for path in sorted(self.folder.rglob("*")):
            if not path.is_file() or path.name in skip or path.suffix.lower() in media:
                continue
            if path.stat().st_size > 4_000_000:
                continue
            relative = path.relative_to(self.folder).as_posix()
            if relative.startswith("Timelines/"):
                continue  # mirrored from the root by `write`
            found[relative] = path.read_bytes()
        return found

    def timeline_files(self) -> list[str]:
        """What the sample keeps under `Timelines/<id>/`, by relative name."""
        folders = [p for p in (self.folder / "Timelines").glob("*") if p.is_dir()] if (self.folder / "Timelines").exists() else []
        if not folders:
            return ["draft_info.json", "draft_info.json.bak", "template.tmp", "template-2.tmp",
                    "attachment/patch/patch.json"]
        return sorted(p.relative_to(folders[0]).as_posix() for p in folders[0].rglob("*") if p.is_file())


def _text_material(sample: Sample, text: str, words: list[dict], at: float) -> dict:
    """A plain text — no template, no animation, the app's default font."""
    material = copy.deepcopy(sample.text_material) if sample.text_material else {
        "type": "text", "content": "", "text_color": "#ffffffff", "font_size": 20.0, "text_size": 30,
        "alignment": 1, "line_spacing": 0.02, "letter_spacing": 0.0, "words": {}, "check_flag": 7,
    }
    material["id"] = _uuid()
    material["name"] = _uuid()
    for key in ("font_path", "font_id", "font_name", "font_resource_id", "font_url", "font_team_id",
                "font_third_resource_id", "font_category_id", "font_category_name", "recognize_task_id",
                "recognize_text", "language", "base_content"):
        if key in material:
            material[key] = ""
    material["font_title"] = "none"
    for key in ("fonts", "text_to_audio_ids", "relevance_segment"):
        if key in material:
            material[key] = []
    try:
        content = json.loads(material.get("content") or "")
        styles = content.get("styles") or []
    except (TypeError, ValueError):
        content, styles = {}, []
    if not styles:
        styles = [{"fill": {"content": {"solid": {"color": [1.0, 1.0, 1.0]}, "render_type": "solid"}},
                   "size": 20, "useLetterColor": True,
                   "strokes": [{"width": 0.032, "mode": 0, "content": {"solid": {"color": [0, 0, 0]}, "render_type": "solid"}}]}]
    for style in styles:
        style.pop("font", None)
        style["range"] = [0, len(text)]
    material["content"] = json.dumps({**content, "styles": styles, "text": text}, ensure_ascii=False)
    starts, ends, tokens = [], [], []
    for index, word in enumerate(words):
        if index:
            joint = round((float(words[index - 1]["end"]) - at) * 1000)
            starts.append(joint); ends.append(joint); tokens.append(" ")
        starts.append(round((float(word["t"]) - at) * 1000))
        ends.append(round((float(word["end"]) - at) * 1000))
        tokens.append(str(word["s"]))
    material["words"] = {"start_time": starts, "end_time": ends, "text": tokens}
    return material


def draft(
    base: pathlib.Path, name: str, *, sample: Sample, drafts_root: str,
    read_json, caption_groups, now: float | None = None,
) -> tuple[dict, dict, str]:
    """`draft_info.json`, `draft_meta_info.json` and the raw file's name for a
    draft called `name` that will live at `drafts_root/name`."""
    project = read_json(base / "project.json")
    files = project["files"]
    timeline = read_json(base / files["timeline"])
    canvas = project["canvas"]
    fps = int(canvas.get("fps", 30))
    clips = [dict(clip) for clip in timeline["clips"]]
    retime.snap_to_frames(clips, fps)
    raw = base / timeline["source"]
    if not raw.exists():
        raise NotFound(f"il girato non c'è: {timeline['source']}")
    source = project.get("source") or {}
    raw_seconds = float(source.get("duration") or max(float(c["end"]) for c in clips))
    width, height = int(canvas["width"]), int(canvas["height"])
    stamp = now if now is not None else time.time()
    folder = f"{drafts_root.rstrip('/')}/{name}"
    raw_name = "raw" + raw.suffix.lower()
    raw_path = f"{folder}/{raw_name}"
    timeline_id = _uuid()

    info = copy.deepcopy(sample.skeleton)
    info["id"] = timeline_id
    info["canvas_config"] = {"ratio": "original", "width": width, "height": height, "background": None}
    info["fps"] = float(fps)
    info["duration"] = 0
    info["platform"] = copy.deepcopy(sample.platform)
    info["config"] = copy.deepcopy(sample.config)
    info["draft_type"] = "video"
    info["name"] = ""
    if sample.version is not None:
        info["version"] = sample.version
    if sample.new_version is not None:
        info["new_version"] = sample.new_version
    materials = info.setdefault("materials", {})

    # The raw footage, imported once.
    local_id = str(uuid.uuid4())
    video_material = copy.deepcopy(sample.video_material)
    video_material.update({
        "id": _uuid(), "unique_id": uuid.uuid4().hex, "type": "video",
        "duration": round(raw_seconds * US), "path": raw_path, "media_path": "",
        "width": int(source.get("width") or width), "height": int(source.get("height") or height),
        "material_name": raw_name, "local_material_id": local_id, "has_audio": True,
    })
    materials.setdefault("videos", []).append(video_material)

    # One segment per clip of the cut, end to end.
    video_track = {**copy.deepcopy(sample.video_track), "id": _uuid(), "segments": []}
    cursor = 0
    for index, clip in enumerate(clips):
        length = round(int(clip["frames"]) / fps * US)
        extras = []
        for kind, template in sample.video_extras.items():
            item = copy.deepcopy(template)
            item["id"] = _uuid()
            materials.setdefault(kind, []).append(item)
            extras.append(item["id"])
        segment = copy.deepcopy(sample.video_segment)
        segment.update({
            "id": _uuid(), "material_id": video_material["id"], "extra_material_refs": extras,
            "source_timerange": {"start": round(float(clip["start"]) * US), "duration": length},
            "target_timerange": {"start": cursor, "duration": length},
            "render_timerange": {"start": 0, "duration": 0},
            "speed": 1.0, "volume": 1.0, "last_nonzero_volume": 1.0, "visible": True,
            "render_index": 0, "track_render_index": 0, "keyframe_refs": [], "common_keyframes": [],
            "desc": str(clip.get("label") or f"clip {index + 1}")[:60],
        })
        if isinstance(segment.get("clip"), dict):
            segment["clip"] = {"scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0, "transform": {"x": 0.0, "y": 0.0},
                               "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0}
        video_track["segments"].append(segment)
        cursor += length
    info["duration"] = cursor
    info["tracks"] = [video_track]

    # The captions, as the film shows them: one text per on-screen line.
    words = read_json(base / files["captions"]) if (base / files["captions"]).exists() else []
    cards = read_json(base / files["cards"]) if (base / files["cards"]).exists() else []
    groups = caption_groups(words, cards, cursor / US) if words else []
    if groups:
        text_track = {**copy.deepcopy(sample.text_track), "id": _uuid(), "segments": []}
        for number, group in enumerate(groups):
            text = " ".join(str(w["s"]) for w in group).strip()
            if not text:
                continue
            start, end = float(group[0]["t"]), float(group[-1]["end"])
            material = _text_material(sample, text, group, start)
            materials.setdefault("texts", []).append(material)
            segment = copy.deepcopy(sample.text_segment)
            segment.update({
                "id": _uuid(), "material_id": material["id"], "extra_material_refs": [],
                "source_timerange": None,
                "target_timerange": {"start": round(start * US), "duration": max(round((end - start) * US), US // 10)},
                "render_timerange": {"start": 0, "duration": 0},
                "render_index": 14000 + number, "track_render_index": 1, "keyframe_refs": [], "common_keyframes": [],
                "visible": True, "desc": "",
            })
            text_track["segments"].append(segment)
        if text_track["segments"]:
            info["tracks"].append(text_track)

    meta = copy.deepcopy(sample.meta)
    meta.update({
        "draft_id": _uuid(), "draft_name": name, "draft_fold_path": folder, "draft_root_path": drafts_root.rstrip("/"),
        "draft_cover": "draft_cover.jpg", "tm_draft_create": round(stamp * US), "tm_draft_modified": round(stamp * US),
        "tm_draft_removed": 0, "tm_duration": cursor, "draft_timeline_materials_size_": raw.stat().st_size,
        "draft_materials_copied_info": [], "draft_segment_extra_info": [],
        "draft_cloud_last_action_download": False, "cloud_draft_sync": False,
    })
    entry = copy.deepcopy(sample.meta_material)
    entry.update({
        "id": local_id, "file_Path": raw_path, "extra_info": raw_name, "metetype": "video", "type": 0,
        "duration": round(raw_seconds * US), "width": video_material["width"], "height": video_material["height"],
        "create_time": round(stamp), "import_time": round(stamp), "import_time_ms": round(stamp * US),
        "roughcut_time_range": {"start": 0, "duration": round(raw_seconds * US)},
        "sub_time_range": {"start": -1, "duration": -1}, "item_source": 1, "md5": "",
    })
    buckets = meta.setdefault("draft_materials", [])
    bucket = next((b for b in buckets if b.get("type") == 0), None)
    if bucket is None:
        bucket = {"type": 0, "value": []}
        buckets.insert(0, bucket)
    bucket["value"] = [entry]
    for other in buckets:
        if other is not bucket:
            other["value"] = []
    return info, meta, raw_name


def write(
    base: pathlib.Path, name: str, target: pathlib.Path, *, sample: Sample, drafts_root: str,
    read_json, caption_groups, ffmpeg: str | None = None, now: float | None = None,
) -> pathlib.Path:
    """The whole draft folder, zipped at `target`: unzip it into the drafts
    root and CapCut lists it. The raw footage travels inside."""
    info, meta, raw_name = draft(base, name, sample=sample, drafts_root=drafts_root,
                                 read_json=read_json, caption_groups=caption_groups, now=now)
    timeline_id = info["id"]
    project = read_json(base / "project.json")
    raw = base / read_json(base / project["files"]["timeline"])["source"]
    stamp = round(now if now is not None else time.time())
    dumped = json.dumps(info, ensure_ascii=False)
    segments = [segment["id"] for track in info["tracks"] for segment in track["segments"]]
    id_mapping = {"id_mapping": {
        "mapping": [{"short_id": str(1000 + n), "uuid": uid} for n, uid in enumerate(segments)],
        "next_index": 1000 + len(segments), "version": "1.0.0",
    }}
    texts = {
        "draft_info.json": dumped,
        "draft_info.json.bak": dumped,
        "template-2.tmp": dumped,
        "common_attachment/attachment_id_mapping.json": json.dumps(id_mapping),
        "draft_meta_info.json": json.dumps(meta, ensure_ascii=False),
        "draft_settings": f"[General]\ncloud_last_modify_platform=mac\ndraft_create_time={stamp}\n"
                          f"draft_last_edit_time={stamp}\nreal_edit_keys=0\nreal_edit_seconds=0\n",
        "timeline_layout.json": json.dumps({"dockItems": [{"dockIndex": 0, "ratio": 1, "timelineIds": [timeline_id],
                                                            "timelineNames": ["Timeline 01"]}], "layoutOrientation": 1}),
        "draft_biz_config.json": json.dumps({"timeline_settings": {timeline_id: {"linkage_enabled": False}}, "track_settings": {}}),
        "draft_virtual_store.json": json.dumps({"draft_materials": [], "draft_virtual_store": [
            {"type": 0, "value": []},
            {"type": 1, "value": [{"child_id": meta["draft_materials"][0]["value"][0]["id"], "parent_id": ""}]},
            {"type": 2, "value": []}]}),
        "draft_agency_config.json": json.dumps({"is_auto_agency_enabled": False, "is_auto_agency_popup": False,
                                                "is_single_agency_mode": False, "marterials": None,
                                                "use_converter": False, "video_resolution": 720}),
        "performance_opt_info.json": json.dumps({"manual_cancle_precombine_segs": None, "need_auto_precombine_segs": None}),
        "key_value.json": "{}",
        ".locked": "",
        "attachment/patch/patch.json": json.dumps({"patch_data": []}),
        "common_attachment/attachment_pc_timeline.json": json.dumps({"reference_lines_config": {
            "horizontal_lines": [], "is_lock": False, "is_visible": False, "vertical_lines": []}, "safe_area_type": 0}),
    }
    files: dict[str, bytes] = {name: body.encode("utf-8") for name, body in texts.items()}
    # CapCut's own companions — attachments, `draft.extra` — as the sample has them.
    for relative, body in sample.companions().items():
        files.setdefault(relative, body)
    files["template.tmp"] = json.dumps(sample.empty_timeline, ensure_ascii=False).encode("utf-8")
    # The timeline folder mirrors the root, file for file, as CapCut keeps it;
    # the empty timeline and the patch list live only there.
    for relative in sample.timeline_files():
        if relative in files:
            files[f"Timelines/{timeline_id}/{relative}"] = files[relative]
    for only_there in ("template.tmp", "attachment/patch/patch.json"):
        files.pop(only_there, None)
    cover = None
    if ffmpeg:
        cover = io.BytesIO()
        try:
            cover.write(subprocess.run(
                [ffmpeg, "-v", "error", "-ss", f"{float(info['tracks'][0]['segments'][0]['source_timerange']['start']) / US:.3f}",
                 "-i", str(raw), "-frames:v", "1", "-vf", "scale=540:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
                capture_output=True, check=True).stdout)
        except (subprocess.CalledProcessError, OSError):
            cover = None
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, body in files.items():
            archive.writestr(f"{name}/{relative}", body)
        if cover is not None and cover.getvalue():
            archive.writestr(f"{name}/draft_cover.jpg", cover.getvalue())
            archive.writestr(f"{name}/Timelines/{timeline_id}/draft_cover.jpg", cover.getvalue())
        for empty in ("subdraft/", "adjust_mask/", "matting/", "qr_upload/", "smart_crop/",
                      "Resources/audioAlg/", "Resources/digitalHuman/", "Resources/videoAlg/"):
            archive.writestr(f"{name}/{empty}", "")
        archive.write(raw, f"{name}/{raw_name}", compress_type=zipfile.ZIP_STORED)
    return target
