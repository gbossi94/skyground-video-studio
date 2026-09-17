import { describe, expect, it } from "vitest";
import type { Word } from "../types";
import type { EditState } from "./model";
import { build, clipAt, joins, outputAt, sourceAt, totalRemoved, type CutSource } from "./sequence";

function speak(start: number, text: string, pace = 0.3, gap = 0.1): Word[] {
  let at = start;
  return text.split(" ").map((s) => {
    const word = { t: round(at), end: round(at + pace), s, p: 0.95 };
    at += pace + gap;
    return word;
  });
}
const round = (value: number) => Math.round(value * 1000) / 1000;

//  0        2        4        6        8       10
//  [ prima ]         [ seconda ]       [ terza ]
const WORDS: Word[] = [...speak(1.0, "prima parte"), ...speak(4.0, "seconda parte"), ...speak(8.0, "terza parte")];
const SOURCE: CutSource = {
  removed: [{ start: 2.0, end: 4.0, reason: "retake", detail: "ripetizione dell'apertura" }],
  engineKept: [[0, 1], [2, 3], [4, 5]],
  sourceDuration: 12,
};
// The middle sentence was taken out by hand; the engine had kept it.
const STATE: EditState = {
  kept: [
    { first: 0, last: 1, start: 0.9, end: 1.8 },
    { first: 4, last: 5, start: 7.9, end: 8.8 },
  ],
};

describe("the sequence", () => {
  it("butts the clips against each other in the film's own time", () => {
    const sequence = build(STATE, WORDS, SOURCE);
    const spans = sequence.clips.map((clip) => [clip.outputStart, clip.outputEnd]);
    expect(spans[0][0]).toBe(0);
    expect(spans[0][1]).toBeCloseTo(0.9, 6);
    expect(spans[1][0]).toBeCloseTo(0.9, 6);
    expect(spans[1][1]).toBeCloseTo(1.8, 6);
    expect(sequence.duration).toBeCloseTo(1.8, 6);
    expect(totalRemoved(sequence)).toBeCloseTo(12 - 1.8, 6);
  });

  it("marks every join, and says why the piece is missing", () => {
    const sequence = build(STATE, WORDS, SOURCE);
    expect(sequence.cuts.map((cut) => cut.index)).toEqual([0, 1, 2]);
    // Before the first word: dead air.
    expect(sequence.cuts[0]).toMatchObject({ reason: "lead-in", at: 0 });
    // The middle: words the engine kept, taken out by hand.
    expect(sequence.cuts[1]).toMatchObject({ reason: "manual", at: sequence.clips[1].outputStart });
    expect(sequence.cuts[1].detail).toContain("seconda");
    expect(sequence.cuts[1].removed).toBeCloseTo(7.9 - 1.8, 6);
    // After the last: dead air again.
    expect(sequence.cuts[2]).toMatchObject({ reason: "lead-out", at: sequence.duration });
  });

  it("keeps the engine's reason for material the engine removed", () => {
    // Only the first sentence is kept; the engine had not kept words 2..3.
    const engineDropped: CutSource = { ...SOURCE, engineKept: [[0, 1], [4, 5]] };
    const sequence = build({ kept: [STATE.kept[0], STATE.kept[1]] }, WORDS, engineDropped);
    expect(sequence.cuts[1]).toMatchObject({ reason: "retake", detail: "ripetizione dell'apertura" });
  });

  it("maps film time to source time and back", () => {
    const sequence = build(STATE, WORDS, SOURCE);
    expect(sourceAt(sequence, 0)).toBeCloseTo(0.9, 6);
    expect(sourceAt(sequence, 0.5)).toBeCloseTo(1.4, 6);
    expect(sourceAt(sequence, 0.9)).toBeCloseTo(7.9, 6); // over the join
    expect(sourceAt(sequence, 99)).toBeCloseTo(8.8, 6);
    expect(outputAt(sequence, 1.4)).toBeCloseTo(0.5, 6);
    expect(outputAt(sequence, 8.4)).toBeCloseTo(1.4, 6);
    // Material that was cut belongs to the join it was cut out of.
    expect(outputAt(sequence, 5.0)).toBeCloseTo(0.9, 6);
    expect(outputAt(sequence, 0.1)).toBe(0);
  });

  it("finds the clip under an instant, and lists the joins", () => {
    const sequence = build(STATE, WORDS, SOURCE);
    expect(clipAt(sequence, 0.4)?.index).toBe(0);
    expect(clipAt(sequence, 1.5)?.index).toBe(1);
    expect(clipAt(sequence, 5)).toBeNull();
    const points = joins(sequence);
    expect(points[0]).toBe(0);
    expect(points[1]).toBeCloseTo(0.9, 6);
    expect(points[2]).toBeCloseTo(1.8, 6);
  });

  it("an empty edit is an empty film, not a crash", () => {
    const sequence = build({ kept: [] }, WORDS, SOURCE);
    expect(sequence.clips).toEqual([]);
    expect(sequence.duration).toBe(0);
    expect(sourceAt(sequence, 3)).toBe(0);
    expect(sequence.cuts).toHaveLength(1);
    expect(sequence.cuts[0].removed).toBe(12);
  });
});
