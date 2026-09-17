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
    assert ranges[0] == (round(8.376 * 29.97), round(8.376 * 29.97) + round(162 / 30 * 29.97) - 1)
    assert ranges[1][0] == round(13.907 * 29.97)
    # 25.575 s → 767 timeline frames → 766 raw frames at 29.97, last inclusive
    assert ranges[2] == (900, 900 + round(767 / 30 * 29.97) - 1)
    # At 30 fps the raw frames are the timeline frames.
    assert bridge.clip_ranges(TIMELINE, 30)[0] == (251, 251 + 162 - 1)


class Item:
    def __init__(self, path, fps="29.97"):
        self.path, self.fps = path, fps

    def GetClipProperty(self, key):
        return self.fps if key == "FPS" else None


class Timeline:
    def __init__(self, name):
        self.name, self.items = name, []

    def GetName(self):
        return self.name


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
    assert project.settings["timelineResolutionWidth"] == "1080"
    assert project.settings["timelineResolutionHeight"] == "1920"
    assert project.settings["timelineFrameRate"] == "30"
    assert resolve.storage.added == [str(tmp_path / "raw.mov")]
    clips, subtitles = project.pool.appended
    assert [(c["startFrame"], c["endFrame"]) for c in clips] == bridge.clip_ranges(TIMELINE, 29.97)
    assert all(c["mediaPoolItem"].path == str(tmp_path / "raw.mov") for c in clips)
    assert result == {"project": "TEST", "timeline": "Skyground", "clips": 3, "clip_fps": 29.97, "subtitles": "sì"}
    assert project.pool.imported == [str(tmp_path / "test.srt")]


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
