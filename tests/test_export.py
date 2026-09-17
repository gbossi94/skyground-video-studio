"""The cut handed to an editor a person already knows: FCPXML for Resolve,
Premiere and Final Cut, SRT for the captions."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from skyground.core import export

PROJECT = "beauty-centers-growth-01"


def parse(document: str) -> ET.Element:
    # ElementTree does not take the DOCTYPE line the editors expect.
    return ET.fromstring("\n".join(line for line in document.splitlines() if not line.startswith("<!DOCTYPE")))


def rational(value: str) -> int:
    number, scale = value[:-1].split("/")
    assert int(scale) == export.SCALE
    return int(number)


def test_the_cut_becomes_one_sequence_with_a_clip_per_clip(workspace):
    root = parse(workspace.export_fcpxml(PROJECT))
    clips = root.findall(".//asset-clip")
    assert len(clips) == 24
    asset = root.find(".//asset")
    assert asset.get("name") == "raw.mov"
    assert asset.find("media-rep").get("src").endswith("/assets/raw.mov")
    sequence = root.find(".//sequence")
    assert sequence.get("format") == root.find(".//format").get("id")


def test_clips_sit_end_to_end_on_whole_frames(workspace):
    root = parse(workspace.export_fcpxml(PROJECT))
    frame = rational(root.find(".//format").get("frameDuration"))
    assert frame == export.SCALE // 30
    expected = 0
    for clip in root.findall(".//asset-clip"):
        assert rational(clip.get("offset")) == expected, "un buco o una sovrapposizione fra due clip"
        duration = rational(clip.get("duration"))
        assert duration % frame == 0, "una clip che non dura un numero intero di fotogrammi"
        expected += duration
    assert rational(root.find(".//sequence").get("duration")) == expected


def test_the_first_clip_starts_where_the_timeline_says(workspace):
    root = parse(workspace.export_fcpxml(PROJECT))
    first = root.findall(".//asset-clip")[0]
    # The reference edit opens at 4.5s of the raw footage.
    assert rational(first.get("start")) == 4.5 * export.SCALE


def test_the_captions_become_subrip_cues_in_output_time(workspace):
    document = workspace.export_srt(PROJECT)
    cues = re.findall(r"(\d+)\n(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})\n(.+)\n", document)
    assert len(cues) > 50
    assert [int(number) for number, *_ in cues] == list(range(1, len(cues) + 1))
    assert cues[0][3].startswith("Se il tuo")
    assert cues[0][1] == "00:00:00,000"
    ends = [end for _, _, end, _ in cues]
    assert ends == sorted(ends)
    assert ends[-1] < "00:01:56,000"  # the film is 115.9 s long


def test_export_writes_into_the_project_folder(workspace):
    written = workspace.export(PROJECT, "fcpxml")
    assert written.name == f"{PROJECT}.fcpxml"
    assert written.read_text(encoding="utf-8").startswith('<?xml version="1.0"')
    assert workspace.export(PROJECT, "srt").read_text(encoding="utf-8").startswith("1\n")


def test_the_export_is_downloaded_from_the_api(client, sign_in, registered_project):
    sign_in("viewer@skyground.online")  # reading the cut is enough to take it away
    response = client.get(f"/api/projects/{PROJECT}/export/fcpxml")
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == f'attachment; filename="{PROJECT}.fcpxml"'
    assert len(parse(response.text).findall(".//asset-clip")) == 24
    assert client.get(f"/api/projects/{PROJECT}/export/srt").text.startswith("1\n")
    assert client.get(f"/api/projects/{PROJECT}/export/edl").status_code in (400, 422)
