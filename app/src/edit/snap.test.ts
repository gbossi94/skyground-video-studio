import { describe, expect, it } from "vitest";
import type { Word } from "../types";
import { candidates, quantiseToFrame, snap, wordAround, type SnapContext } from "./snap";

const WORDS: Word[] = [
  { t: 1.0, end: 1.3, s: "una", p: 0.9 },
  { t: 1.4, end: 1.8, s: "parola", p: 0.9 },
  { t: 3.0, end: 3.4, s: "dopo", p: 0.9 },
];
const CONTEXT: SnapContext = {
  words: WORDS,
  silences: [{ start: 1.85, end: 2.95 }],
  fps: 30,
  duration: 5,
};
const POINTS = candidates(WORDS, CONTEXT.silences);

describe("snap", () => {
  it("lists every word and silence edge once, sorted", () => {
    expect(POINTS).toEqual([1.0, 1.3, 1.4, 1.8, 1.85, 2.95, 3.0, 3.4]);
  });

  it("takes a candidate within the magnet, measured in pixels", () => {
    // 100 px/s, 8 px magnet → 0.08 s reach.
    expect(snap(1.36, CONTEXT, POINTS, 100)).toBe(1.4);
    expect(snap(2.5, CONTEXT, POINTS, 100)).toBe(quantiseToFrame(2.5, 30));
    // Zoomed out, the same distance in seconds is inside the magnet.
    expect(snap(2.5, CONTEXT, POINTS, 10)).toBe(2.95);
  });

  it("never rests inside a word, magnet or not", () => {
    expect(wordAround(WORDS, 1.6)).toBe(1);
    expect(snap(1.6, CONTEXT, POINTS, 1000)).toBe(1.8);
    expect(snap(1.45, CONTEXT, POINTS, 1000)).toBe(1.4);
  });

  it("lands on whole frames in the open, unless that would touch a word", () => {
    expect(snap(2.505, CONTEXT, POINTS, 1000)).toBe(2.5);
    // 3.01 quantises to 3.0 which is the edge of a word: allowed (an edge is fine).
    expect(snap(3.01, CONTEXT, POINTS, 1000)).toBe(3.0);
  });

  it("stays inside the source", () => {
    expect(snap(-1, CONTEXT, POINTS, 100)).toBe(0);
    expect(snap(9, CONTEXT, POINTS, 100)).toBe(5);
  });
});
