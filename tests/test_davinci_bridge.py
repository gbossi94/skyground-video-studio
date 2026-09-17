"""The Resolve bridge: one file, standard library, run from Resolve's menu.
Resolve itself is not here, so its scripting objects are stood in for by
fakes that record what they were asked; the frame arithmetic is real."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "davinci" / "Skyground.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("skyground_davinci", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TIMELINE = {"source": "assets/raw.mov", "clips": [
    {"start": 8.376, "end": 13.76, "output_start": 0.0, "frames": 162},
    {"start": 13.907, "end": 15.5, "output_start": 5.4, "frames": 48},
    {"start": 30.025, "end": 55.6, "output_start": 7.0},  # no frames: from end − start
]}


def test_clip_ranges_are_frames_of_the_raw_footage_at_its_own_rate(bridge):
    ranges = bridge.clip_ranges(TIMELINE, 29.97)
    assert ranges[0] == (round(8.376 * 29.97), round(8.376 * 29.97) + round(162 / 30 * 29.97))
    assert ranges[1][0] == round(13.907 * 29.97)
    # 25.575 s → 767 timeline frames → 766 raw frames at 29.97, end exclusive
    assert ranges[2] == (900, 900 + round(767 / 30 * 29.97))
    # At 30 fps the raw frames are the timeline frames: 162 of them, end exclusive,
    # which is what Resolve 21 lands as 162 (with an inclusive end it landed 161).
    assert bridge.clip_ranges(TIMELINE, 30)[0] == (251, 251 + 162)


class Item:
    def __init__(self, path, fps="29.97"):
        self.path, self.fps = path, fps

    def GetClipProperty(self, key):
        return self.fps if key == "FPS" else None


class Timeline:
    def __init__(self, name):
        self.name, self.items, self.tracks = name, [], []

    def GetName(self):
        return self.name

    def AddTrack(self, kind):
        self.tracks.append(kind)
        return True


class Pool:
    def __init__(self):
        self.timelines, self.appended, self.imported = {}, [], []

    def CreateEmptyTimeline(self, name):
        if name in self.timelines:
            return None
        self.timelines[name] = Timeline(name)
        return self.timelines[name]

    def AppendToTimeline(self, infos):
        self.appended.append(infos)
        return list(infos)

    def ImportMedia(self, paths):
        self.imported.extend(paths)
        return [Item(p, "0") for p in paths]


class Project:
    def __init__(self, name):
        self.name, self.settings, self.pool = name, {}, Pool()

    def SetSetting(self, key, value):
        self.settings[key] = value
        return True

    def GetMediaPool(self):
        return self.pool


class Manager:
    def __init__(self):
        self.projects = {}

    def LoadProject(self, name):
        return self.projects.get(name)

    def CreateProject(self, name):
        self.projects[name] = Project(name)
        return self.projects[name]


class Storage:
    def __init__(self):
        self.added = []

    def AddItemListToMediaPool(self, paths):
        self.added.extend(paths)
        return [Item(p) for p in paths]


class Resolve:
    def __init__(self):
        self.manager, self.storage = Manager(), Storage()

    def GetProjectManager(self):
        return self.manager

    def GetMediaStorage(self):
        return self.storage


def fetched(tmp_path):
    return {"project": {"canvas": {"width": 1080, "height": 1920, "fps": 30}}, "timeline": TIMELINE,
            "raw": tmp_path / "raw.mov", "subtitles": tmp_path / "test.srt", "fcpxml": tmp_path / "test.fcpxml"}


def test_build_makes_the_project_the_timeline_and_the_clips(bridge, tmp_path):
    resolve = Resolve()
    result = bridge.build(resolve, "TEST", fetched(tmp_path))
    project = resolve.manager.projects["TEST"]
    assert project.settings == {"timelineFrameRate": "30", "timelineResolutionWidth": "1080",
                                "timelineResolutionHeight": "1920"}  # playback rate is read-only
    assert resolve.storage.added == [str(tmp_path / "raw.mov")]
    clips, subtitles = project.pool.appended
    assert [(c["startFrame"], c["endFrame"]) for c in clips] == bridge.clip_ranges(TIMELINE, 29.97)
    assert all(c["mediaPoolItem"].path == str(tmp_path / "raw.mov") for c in clips)
    assert result == {"project": "TEST", "timeline": "Skyground", "clips": 3, "clip_fps": 29.97, "subtitles": "sì"}
    assert project.pool.imported == [str(tmp_path / "test.srt")]
    # Resolve only lands an SRT on a timeline that already has a subtitle track.
    assert project.pool.timelines["Skyground"].tracks == ["subtitle"]


def test_a_second_run_never_overwrites_the_timeline_a_hand_may_have_touched(bridge, tmp_path):
    resolve = Resolve()
    bridge.build(resolve, "TEST", fetched(tmp_path))
    result = bridge.build(resolve, "TEST", fetched(tmp_path))
    assert result["timeline"] == "Skyground 2"
    assert len(resolve.manager.projects) == 1  # the same project, reopened


def test_fetch_takes_the_cut_the_footage_and_the_captions(bridge, tmp_path):
    class Studio:
        def __init__(self):
            self.downloaded = []

        def document(self, project, name):
            return {"project.json": {"canvas": {"width": 1080, "height": 1920, "fps": 30}},
                    "timeline.json": TIMELINE}[name]

        def download(self, path, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
            self.downloaded.append(path)
            return target

    studio = Studio()
    got = bridge.fetch(studio, "test-260917", tmp_path / "test-260917")
    assert got["raw"] == tmp_path / "test-260917" / "raw.mov"
    assert studio.downloaded == ["/media/test-260917/assets/raw.mov",
                                 "/api/projects/test-260917/export/srt",
                                 "/api/projects/test-260917/export/fcpxml"]
    # The footage is kept: a second run does not download it again.
    bridge.fetch(studio, "test-260917", tmp_path / "test-260917")
    assert studio.downloaded.count("/media/test-260917/assets/raw.mov") == 1


def test_the_script_is_standard_library_only(bridge):
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("import requests", "import skyground", "from skyground"):
        assert forbidden not in text


def test_the_manifest_is_the_cut_as_a_lua_table_for_the_resolve_side(bridge, tmp_path):
    got = fetched(tmp_path)
    text = bridge.manifest(got, 'Prova "uno"')
    assert text.startswith("-- written by Skyground.py")
    assert 'name = "Prova \\"uno\\"",' in text  # quotes survive as a Lua literal
    assert "width = 1080, height = 1920, fps = 30," in text
    assert f'raw = "{tmp_path / "raw.mov"}",' in text
    assert f'subtitles = "{tmp_path / "test.srt"}",' in text
    # Each clip: seconds into the raw footage, and the timeline frames the studio decided on.
    assert "{start = 8.376, frames = 162}," in text
    assert "{start = 30.025, frames = 767}," in text  # no frames given: from end − start
    assert text.count("{start = ") == 3


def test_write_manifest_puts_the_cut_beside_the_footage_and_a_copy_where_lua_looks(bridge, tmp_path):
    folder = tmp_path / "Skyground" / "test-260917"
    folder.mkdir(parents=True)
    cut = bridge.write_manifest(fetched(tmp_path), "TEST", folder)
    assert cut == folder / "cut.lua"
    assert (tmp_path / "Skyground" / "latest.lua").read_text() == cut.read_text()


def test_the_lua_side_reads_what_the_python_side_writes():
    """The two halves agree on the manifest's field names."""
    lua = (SCRIPT.parent / "Skyground.lua").read_text(encoding="utf-8")
    for field in ("cut.name", "cut.width", "cut.height", "cut.fps", "cut.raw", "cut.subtitles", "cut.clips", "clip.start", "clip.frames"):
        assert field in lua
    assert "latest.lua" in lua
