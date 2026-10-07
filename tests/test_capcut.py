"""A CapCut draft written from the cut, shaped after a sample CapCut saved.

The sample here is a hand-made stand-in with the fields the generator reads:
the real one is workspace data, uploaded by the person whose CapCut it is,
and never in the repository.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import zipfile

import pytest

from skyground.core import capcut
from skyground.core.workspace import Workspace, caption_groups, read_json
from skyground.errors import NotFound, ValidationError

PROJECT = "beauty-centers-growth-01"
ROOT = "/Users/qualcuno/Movies/CapCut/User Data/Projects/com.lveditor.draft"


def make_sample(folder: pathlib.Path, *, with_text: bool = True, with_template: bool = True) -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    segment = {
        "id": "SEG", "material_id": "VID", "extra_material_refs": ["SPD", "CNV"],
        "source_timerange": {"start": 1_000_000, "duration": 2_000_000},
        "target_timerange": {"start": 0, "duration": 2_000_000},
        "render_timerange": {"start": 0, "duration": 0}, "speed": 1.0, "volume": 0.5,
        "clip": {"scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0, "transform": {"x": 0.0, "y": 0.0},
                 "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0},
        "render_index": 0, "track_render_index": 0, "visible": True, "hdr_settings": {"mode": 1},
        "enable_lut": True, "enable_adjust": True, "keyframe_refs": [], "common_keyframes": [], "desc": "",
    }
    text_segment = {**segment, "id": "TSEG", "material_id": "TXT", "extra_material_refs": ["ANIM"],
                    "source_timerange": None, "hdr_settings": None,
                    "clip": {"scale": {"x": 0.6, "y": 0.6}, "rotation": 0.0, "transform": {"x": 0.0, "y": -0.4},
                             "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0},
                    "render_index": 14000, "track_render_index": 1}
    materials = {
        "videos": [{"id": "VID", "unique_id": "abc", "type": "video", "duration": 10_000_000,
                    "path": "/Users/qualcuno/Downloads/IMG_1.MOV", "width": 1080, "height": 1920,
                    "material_name": "IMG_1.MOV", "local_material_id": "local-1", "has_audio": True, "crop_ratio": "free"}],
        "speeds": [{"id": "SPD", "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None}],
        "canvases": [{"id": "CNV", "type": "canvas_color", "color": "", "blur": 0.0}],
        "texts": [{"id": "TXT", "type": "text", "name": "x", "font_path": "/Users/qualcuno/Library/f.ttf",
                   "font_id": "7", "font_title": "Fredoka", "fonts": [{"path": "/Users/qualcuno/Library/f.ttf"}],
                   "content": json.dumps({"styles": [{"fill": {"content": {"solid": {"color": [1, 1, 1]}}},
                                                       "range": [0, 4], "size": 20, "font": {"path": "/x", "id": "7"}}],
                                          "text": "ciao"}),
                   "words": {"start_time": [0], "end_time": [400], "text": ["ciao"]},
                   "language": "es-MX", "text_color": "#ffffffff", "font_size": 20.0}] if with_text else [],
        "material_animations": [{"id": "ANIM", "type": "sticker_animation", "animations": []}],
        "audios": [],
    }
    info = {
        "id": "TL", "canvas_config": {"ratio": "original", "width": 1080, "height": 1920, "background": None},
        "fps": 30.0, "duration": 2_000_000, "version": 360000, "new_version": "185.0.0",
        "platform": {"os": "mac", "app_version": "9.4.0", "device_id": "d"},
        "config": {"video_mute": False, "subtitle_taskinfo": [{"id": "x"}]},
        "keyframes": {"videos": [], "texts": []},
        "tracks": [
            {"id": "VT", "type": "video", "flag": 0, "attribute": 0, "name": "", "is_default_name": True, "segments": [segment]},
        ] + ([{"id": "TT", "type": "text", "flag": 1, "attribute": 0, "name": "", "is_default_name": True,
               "segments": [text_segment]}] if with_text else []),
        "materials": materials,
    }
    meta = {
        "draft_id": "OLD", "draft_name": "vecchia", "draft_fold_path": ROOT + "/vecchia", "draft_root_path": ROOT,
        "tm_draft_create": 1, "tm_draft_modified": 2, "tm_duration": 2_000_000,
        "draft_materials": [
            {"type": 0, "value": [{"id": "local-1", "file_Path": "/Users/qualcuno/Downloads/IMG_1.MOV", "metetype": "video",
                                   "duration": 10_000_000, "width": 1080, "height": 1920, "type": 0, "extra_info": "IMG_1.MOV",
                                   "roughcut_time_range": {"start": 0, "duration": 10_000_000},
                                   "sub_time_range": {"start": -1, "duration": -1}, "item_source": 1}]},
            {"type": 8, "value": [{"id": "song"}]},
        ],
    }
    (folder / "draft_info.json").write_text(json.dumps(info), encoding="utf-8")
    (folder / "draft_meta_info.json").write_text(json.dumps(meta), encoding="utf-8")
    if with_template:
        skeleton = {**info, "id": "EMPTY", "tracks": [], "materials": {k: [] for k in materials},
                    "canvas_config": {"ratio": "original", "width": 0, "height": 0, "background": None}, "duration": 0}
        (folder / "template.tmp").write_text(json.dumps(skeleton), encoding="utf-8")
    return folder


def stand_in_raw(workspace) -> pathlib.Path:
    """The reference footage lives in a release archive, not in the checkout:
    a few bytes under its name are all the generator needs to look at."""
    raw = workspace.project_dir(PROJECT) / "assets" / "raw.mov"
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists():
        raw.write_bytes(b"non un video, ma abbastanza per essere impacchettato")
    return raw


def generate(workspace, tmp_path, **kwargs):
    stand_in_raw(workspace)
    sample = capcut.Sample(make_sample(tmp_path / "campione", **kwargs))
    return capcut.draft(workspace.project_dir(PROJECT), "Skyground prova", sample=sample, drafts_root=ROOT,
                        read_json=read_json, caption_groups=caption_groups, now=1_700_000_000.0)


def test_the_cut_becomes_one_video_track_of_segments_end_to_end(workspace, tmp_path):
    info, meta, raw_name = generate(workspace, tmp_path)
    video = next(t for t in info["tracks"] if t["type"] == "video")
    assert len(video["segments"]) == 24
    cursor = 0
    for segment in video["segments"]:
        assert segment["target_timerange"]["start"] == cursor
        assert segment["source_timerange"]["duration"] == segment["target_timerange"]["duration"]
        assert segment["target_timerange"]["duration"] % 1 == 0 and segment["target_timerange"]["duration"] > 0
        cursor += segment["target_timerange"]["duration"]
    assert info["duration"] == cursor == meta["tm_duration"]
    assert abs(cursor / capcut.US - 115.9333) < 0.05
    # The first clip of the reference edit opens at 4.5s of the raw footage.
    assert video["segments"][0]["source_timerange"]["start"] == 4_500_000
    assert raw_name == "raw.mov"


def test_the_raw_footage_is_the_only_material_and_lives_in_the_draft_folder(workspace, tmp_path):
    info, meta, raw_name = generate(workspace, tmp_path)
    videos = info["materials"]["videos"]
    assert len(videos) == 1
    assert videos[0]["path"] == f"{ROOT}/Skyground prova/{raw_name}"
    assert videos[0]["duration"] > 0 and videos[0]["width"] == 1080
    assert all(s["material_id"] == videos[0]["id"] for s in info["tracks"][0]["segments"])
    # The sample's own media never leak into ours.
    assert "IMG_1" not in json.dumps(info) and "IMG_1" not in json.dumps(meta)
    assert meta["draft_fold_path"] == f"{ROOT}/Skyground prova"
    entry = meta["draft_materials"][0]["value"][0]
    assert entry["file_Path"] == videos[0]["path"] and entry["id"] == videos[0]["local_material_id"]
    assert meta["draft_materials"][1]["value"] == []


def test_every_segment_gets_its_own_companions_from_the_sample(workspace, tmp_path):
    info, _, _ = generate(workspace, tmp_path)
    ids = {m["id"] for kind in ("speeds", "canvases") for m in info["materials"][kind]}
    refs = [ref for s in info["tracks"][0]["segments"] for ref in s["extra_material_refs"]]
    assert len(refs) == 48 and len(set(refs)) == 48 and set(refs) == ids


def test_the_captions_become_plain_texts_timed_word_by_word(workspace, tmp_path):
    info, _, _ = generate(workspace, tmp_path)
    text = next(t for t in info["tracks"] if t["type"] == "text")
    assert len(text["segments"]) > 50
    first = text["segments"][0]
    material = next(m for m in info["materials"]["texts"] if m["id"] == first["material_id"])
    content = json.loads(material["content"])
    assert content["text"].startswith("Se il tuo")
    assert "font" not in content["styles"][0] and material["font_path"] == "" and material["fonts"] == []
    assert first["extra_material_refs"] == [] and first["source_timerange"] is None
    assert material["words"]["text"][:3] == ["Se", " ", "il"]
    assert material["words"]["start_time"][0] == 0
    assert first["target_timerange"]["duration"] > 0
    assert first["clip"]["transform"]["y"] == pytest.approx(-0.4)


def test_a_sample_without_texts_still_yields_captions(workspace, tmp_path):
    info, _, _ = generate(workspace, tmp_path, with_text=False)
    assert any(t["type"] == "text" and t["segments"] for t in info["tracks"])


def test_without_capcuts_own_empty_timeline_the_sample_is_emptied_instead(workspace, tmp_path):
    info, _, _ = generate(workspace, tmp_path, with_template=False)
    assert info["config"]["subtitle_taskinfo"] == [{"id": "x"}]  # the sample's config, as CapCut wrote it
    assert len(info["tracks"][0]["segments"]) == 24


def test_a_missing_sample_says_what_it_needs(tmp_path):
    with pytest.raises(NotFound, match="draft_info.json"):
        capcut.Sample(tmp_path / "vuota")


def test_the_zip_holds_the_folder_capcut_expects_with_the_footage_inside(workspace, tmp_path):
    stand_in_raw(workspace)
    sample = capcut.Sample(make_sample(tmp_path / "campione"))
    target = capcut.write(workspace.project_dir(PROJECT), "Skyground prova", tmp_path / "out.zip", sample=sample,
                          drafts_root=ROOT, read_json=read_json, caption_groups=caption_groups, now=1_700_000_000.0)
    names = zipfile.ZipFile(target).namelist()
    info = json.loads(zipfile.ZipFile(target).read("Skyground prova/draft_info.json"))
    for expected in ("draft_info.json", "draft_meta_info.json", "draft_settings", "timeline_layout.json",
                     "draft_virtual_store.json", f"Timelines/{info['id']}/draft_info.json",
                     f"Timelines/{info['id']}/template.tmp", "raw.mov"):
        assert f"Skyground prova/{expected}" in names, expected
    layout = json.loads(zipfile.ZipFile(target).read("Skyground prova/timeline_layout.json"))
    assert layout["dockItems"][0]["timelineIds"] == [info["id"]]


def test_the_workspace_refuses_to_export_without_a_drafts_root(workspace):
    with pytest.raises(ValidationError, match="SKYGROUND_CAPCUT_DRAFTS"):
        workspace.export(PROJECT, "capcut")


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") or os.environ.get("FFMPEG_BIN")),
    reason="la bozza CapCut legge il girato con FFmpeg",
)
def test_the_sample_is_uploaded_by_an_administrator_and_the_draft_downloaded(client, sign_in, make_user, registered_project, workspace, tmp_path):
    stand_in_raw(workspace)
    sample = make_sample(tmp_path / "campione")
    sign_in("owner@skyground.online")  # an owner, not an administrator
    denied = client.put("/api/capcut/sample/draft_info.json", content=(sample / "draft_info.json").read_bytes())
    assert denied.status_code == 403, denied.text

    make_user("admin@skyground.online", is_admin=True)
    sign_in("admin@skyground.online")
    for name in ("draft_info.json", "draft_meta_info.json", "template.tmp"):
        stored = client.put(f"/api/capcut/sample/{name}", content=(sample / name).read_bytes())
        assert stored.status_code == 200, stored.text
    assert client.get("/api/capcut/sample").json()["ready"] is True
    assert client.put("/api/capcut/sample/altro.json", content=b"{}").status_code in (400, 422)

    sign_in("viewer@skyground.online")
    response = client.get(f"/api/projects/{PROJECT}/export/capcut", params={"root": ROOT})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    (tmp_path / "scaricato.zip").write_bytes(response.content)
    archive = zipfile.ZipFile(tmp_path / "scaricato.zip")
    assert any(name.endswith("/draft_info.json") for name in archive.namelist())
