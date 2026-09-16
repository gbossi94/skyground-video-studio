"""Carrying the editorial layers across a change of cut.

Found the way most things here get found: by rendering. The engine produced a
shorter edit, the timeline was rewritten, and the film came out with subtitles
running fourteen seconds ahead of the voice and cards landing on the wrong
sentence — because everything laid on top of the cut is written in output time,
and the cut had moved underneath it.
"""

from __future__ import annotations

import pytest

from skyground.analysis.models import Word
from skyground.core import retime

#: Due pezzi di girato, con un buco di dieci secondi in mezzo.
CUT = [
    {"start": 10.0, "end": 14.0, "output_start": 0.0},
    {"start": 24.0, "end": 30.0, "output_start": 4.0},
]


def test_an_output_moment_is_traced_back_to_the_source():
    assert retime.to_source(0.0, CUT) == 10.0
    assert retime.to_source(5.0, CUT) == 25.0


def test_a_source_moment_is_placed_in_the_output():
    assert retime.to_output(12.0, CUT) == 2.0
    assert retime.to_output(25.0, CUT) == 5.0


def test_a_moment_the_cut_removed_has_nowhere_to_go():
    assert retime.to_output(18.0, CUT) is None


def test_captions_are_written_where_the_words_ended_up():
    words = [
        Word(t=11.0, end=11.4, s="prima", p=0.9),
        Word(t=18.0, end=18.5, s="tolta", p=0.9),
        Word(t=25.0, end=25.6, s="dopo", p=0.9),
    ]

    captions = retime.captions_from(words, CUT)

    assert [item["s"] for item in captions] == ["prima", "dopo"]
    assert captions[0]["t"] == 1.0
    assert captions[1]["t"] == 5.0


def test_a_word_straddling_a_cut_keeps_the_part_that_survived():
    """The sound of it is in the film, so the subtitle has to be too."""
    words = [Word(t=13.5, end=15.0, s="tagliata", p=0.9)]

    captions = retime.captions_from(words, CUT)

    assert len(captions) == 1
    assert captions[0]["end"] == 4.0  # clamped to where the clip ends


def test_a_card_follows_the_moment_it_was_placed_against():
    before = [{"start": 10.0, "end": 30.0, "output_start": 0.0}]
    cards = [{"id": "team", "label": "X", "a": 15.0, "b": 17.0}]

    moved, lost = retime.move_cards(cards, before, CUT)

    assert lost == []
    # Source 25.0, which the new cut places at 5.0. The length is kept.
    assert moved[0]["a"] == 5.0
    assert moved[0]["b"] == 7.0


def test_a_card_over_a_removed_stretch_is_taken_out_and_said_so():
    before = [{"start": 10.0, "end": 30.0, "output_start": 0.0}]
    cards = [{"id": "hours", "label": "12 ore", "a": 8.0, "b": 10.0}]

    moved, lost = retime.move_cards(cards, before, CUT)

    assert moved == []
    assert len(lost) == 1 and "hours" in lost[0]


def test_a_card_squeezed_to_nothing_is_taken_out_too():
    """Half a frame of a motion graphic is a flicker, not a graphic."""
    before = [{"start": 10.0, "end": 30.0, "output_start": 0.0}]
    cards = [{"id": "flash", "label": "X", "a": 19.9, "b": 20.1}]

    moved, lost = retime.move_cards(cards, before, CUT)

    assert moved == []
    assert "flash" in lost[0]


def test_an_angle_insert_moves_but_keeps_its_own_first_frame():
    """Where the insert sits in the film changes; which frame of it plays
    first does not — `mediaStart` is an offset inside its own file."""
    before = [{"start": 10.0, "end": 30.0, "output_start": 0.0}]
    angles = [{"id": "drone", "start": 15.0, "duration": 2.0, "mediaStart": 0.77}]

    moved, lost = retime.move_angles(angles, before, CUT)

    assert lost == []
    assert (moved[0]["start"], moved[0]["duration"]) == (5.0, 2.0)
    assert moved[0]["mediaStart"] == 0.77


def test_output_starts_are_recomputed_when_a_timeline_does_not_carry_them():
    """A timeline written by hand works the same as one written by the engine."""
    written_by_hand = [{"start": 10.0, "end": 14.0}, {"start": 24.0, "end": 30.0}]
    assert retime.spans(written_by_hand) == retime.spans(CUT)


def test_two_cards_that_land_on_each_other_do_not_overlap():
    """A shorter cut can bring two anchors close together. The earlier card
    gives way; it never covers the later one."""
    before = [{"start": 0.0, "end": 40.0, "output_start": 0.0}]
    # Removing 10–29 from the source: a card at 8.0–13.0 still runs to 13.0 in
    # the output, and the card anchored at 31.0 now lands at 12.0 — inside it.
    after = [{"start": 0.0, "end": 10.0, "output_start": 0.0}, {"start": 29.0, "end": 40.0, "output_start": 10.0}]
    cards = [
        {"id": "uno", "label": "A", "a": 8.0, "b": 13.0},
        {"id": "due", "label": "B", "a": 31.0, "b": 34.0},
    ]

    moved, lost = retime.move_cards(cards, before, after)

    assert lost == []
    assert moved[0]["b"] <= moved[1]["a"]
    assert moved[0]["b"] == pytest.approx(moved[1]["a"] - retime.GAP)


def test_a_card_squeezed_out_by_the_next_one_is_dropped_and_said():
    before = [{"start": 0.0, "end": 40.0, "output_start": 0.0}]
    after = [{"start": 0.0, "end": 10.0, "output_start": 0.0}, {"start": 29.9, "end": 40.0, "output_start": 10.0}]
    cards = [
        {"id": "uno", "label": "A", "a": 9.8, "b": 12.0},
        {"id": "due", "label": "B", "a": 30.0, "b": 33.0},
    ]

    moved, lost = retime.move_cards(cards, before, after)

    assert [card["id"] for card in moved] == ["due"]
    assert len(lost) == 1 and "uno" in lost[0] and "posto" in lost[0]
