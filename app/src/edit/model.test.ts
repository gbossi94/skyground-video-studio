import { describe, expect, it } from "vitest";
import type { CutPlan, Word } from "../types";
import {
  boundaries,
  commit,
  fromPlan,
  gapAt,
  historyOf,
  moveBoundary,
  problems,
  rangeAt,
  redo,
  removeRange,
  restoreGap,
  segmentsOf,
  splitRange,
  toRequest,
  trimToPlayhead,
  undo,
  type EditState,
  type Rules,
} from "./model";

/** A sentence laid on a timeline: 0.32 s words with 0.06 s between them. */
function speak(start: number, text: string, pace = 0.32, gap = 0.06): Word[] {
  let at = start;
  return text.split(" ").map((s) => {
    const word = { t: round(at), end: round(at + pace), s, p: 0.95 };
    at += pace + gap;
    return word;
  });
}

const round = (value: number) => Math.round(value * 1000) / 1000;

const WORDS: Word[] = [
  ...speak(3.0, "Se il tuo centro è bloccato sei nel fango."), // 0..8, ends 6.04-ish
  ...speak(8.5, "Se il tuo centro è bloccato sei in quello che chiamo fango."), // 9..20
  ...speak(15.0, "Non importa quanto premi non ti muovi."), // 21..27
];
const DURATION = 20.0;
const RULES: Rules = { leadIn: 0.12, leadOut: 0.28, minSegment: 0.35, duration: DURATION, frame: 1 / 30 };

/** The engine kept the second and third sentences. */
const STATE: EditState = {
  kept: [
    { first: 9, last: 20, start: WORDS[9].t - 0.12, end: WORDS[20].end + 0.28 },
    { first: 21, last: 27, start: WORDS[21].t - 0.12, end: WORDS[27].end + 0.28 },
  ],
};

function plan(): CutPlan {
  return {
    segments: STATE.kept.map((range, index) => ({
      start: range.start, end: range.end, label: `s${index}`, firstWord: range.first, lastWord: range.last,
    })),
  } as unknown as CutPlan;
}

function noWordIsCut(state: EditState) {
  for (const range of state.kept) {
    for (const edge of [range.start, range.end]) {
      for (const word of WORDS) {
        if (word.t + 0.0015 < edge && edge < word.end - 0.0015) {
          throw new Error(`edge ${edge} inside «${word.s}» ${word.t}-${word.end}`);
        }
      }
    }
    // Every word of the range is whole inside it.
    for (let index = range.first; index <= range.last; index += 1) {
      expect(WORDS[index].t).toBeGreaterThanOrEqual(range.start - 0.0015);
      expect(WORDS[index].end).toBeLessThanOrEqual(range.end + 0.0015);
    }
  }
}

describe("in and out", () => {
  it("reads the plan's segments and writes them back unchanged", () => {
    const state = fromPlan(plan(), WORDS);
    expect(state).toEqual(STATE);
    expect(toRequest(state)).toEqual(
      STATE.kept.map((r) => ({ first: r.first, last: r.last, start: round(r.start), end: round(r.end) })),
    );
    expect(segmentsOf(state)).toEqual(STATE.kept.map((r) => ({ start: r.start, end: r.end })));
  });

  it("derives word indices when a segment has none", () => {
    const bare = plan();
    bare.segments[0].firstWord = null;
    bare.segments[0].lastWord = null;
    expect(fromPlan(bare, WORDS).kept[0]).toEqual(STATE.kept[0]);
  });
});

describe("moving an edge", () => {
  it("into a word stops at the edge of that word", () => {
    const inside = WORDS[9].t + 0.1;
    const moved = moveBoundary(STATE, WORDS, 0, "start", inside, RULES)!;
    expect(moved.kept[0].start).toBeCloseTo(WORDS[9].t, 3);
    expect(moved.kept[0].first).toBe(9);
    noWordIsCut(moved);
  });

  it("over words drops or adds them whole", () => {
    // Drag the start past the first three words of the second sentence.
    const past = WORDS[12].t - 0.02;
    const moved = moveBoundary(STATE, WORDS, 0, "start", past, RULES)!;
    expect(moved.kept[0].first).toBe(12);
    expect(moved.kept[0].start).toBeCloseTo(past, 3);
    noWordIsCut(moved);
    // And back again, further than before: the removed sentence's words rejoin.
    const back = moveBoundary(moved, WORDS, 0, "start", WORDS[7].t - 0.02, RULES)!;
    expect(back.kept[0].first).toBe(7);
    noWordIsCut(back);
  });

  it("never crosses the neighbouring range", () => {
    const moved = moveBoundary(STATE, WORDS, 1, "start", 5.0, RULES)!;
    expect(moved.kept[1].first).toBe(21);
    expect(moved.kept[1].start).toBeGreaterThanOrEqual(STATE.kept[0].end - 0.0015);
    noWordIsCut(moved);
  });

  it("refuses to make a range shorter than the minimum", () => {
    const strict = { ...RULES, minSegment: 0.8 };
    expect(moveBoundary(STATE, WORDS, 1, "end", WORDS[21].end + 0.02, strict)).toBeNull();
    expect(moveBoundary(STATE, WORDS, 1, "end", WORDS[21].end + 0.02, RULES)).not.toBeNull();
  });

  it("an end dragged into the air stays where it was put, on the source's grid", () => {
    const moved = moveBoundary(STATE, WORDS, 1, "end", 19.5, RULES)!;
    expect(moved.kept[1].end).toBeCloseTo(19.5, 3);
    expect(moveBoundary(STATE, WORDS, 1, "end", 40, RULES)!.kept[1].end).toBe(DURATION);
  });
});

describe("removing, restoring, splitting", () => {
  it("removes a whole range and restores the gap by joining the neighbours", () => {
    const without = removeRange(STATE, 1)!;
    expect(without.kept).toHaveLength(1);
    expect(gapAt(without, 17.0)).toBe(1);
    const joined = restoreGap(STATE, WORDS, 1, RULES)!;
    expect(joined.kept).toHaveLength(1);
    expect(joined.kept[0]).toEqual({ first: 9, last: 27, start: STATE.kept[0].start, end: STATE.kept[1].end });
    noWordIsCut(joined);
  });

  it("restores the air before the first and after the last range", () => {
    const head = restoreGap(STATE, WORDS, 0, RULES)!;
    expect(head.kept[0].first).toBe(0);
    expect(head.kept[0].start).toBe(0);
    const tail = restoreGap(STATE, WORDS, 2, RULES)!;
    expect(tail.kept[1].last).toBe(27);
    expect(tail.kept[1].end).toBe(DURATION);
  });

  it("splits a range in two whole halves", () => {
    const split = splitRange(STATE, WORDS, 1, 23, RULES)!;
    expect(split.kept).toHaveLength(3);
    expect(split.kept[1].last).toBe(23);
    expect(split.kept[2].first).toBe(24);
    expect(split.kept[1].end).toBeLessThanOrEqual(split.kept[2].start + 0.0015);
    noWordIsCut(split);
    expect(splitRange(STATE, WORDS, 1, 27, RULES)).toBeNull(); // nothing after the last word
  });

  it("trims to the playhead on either side", () => {
    const inAt = WORDS[11].t - 0.02;
    const trimmed = trimToPlayhead(STATE, WORDS, inAt, "start", RULES)!;
    expect(trimmed.kept[0].first).toBe(11);
    const outAt = WORDS[25].end + 0.02;
    const cut = trimToPlayhead(trimmed, WORDS, outAt, "end", RULES)!;
    expect(cut.kept[1].last).toBe(25);
    expect(trimToPlayhead(STATE, WORDS, 7.0, "start", RULES)).toBeNull(); // in a gap
  });
});

describe("looking things up", () => {
  it("finds the range or the gap under an instant", () => {
    expect(rangeAt(STATE, 10.0)).toBe(0);
    expect(rangeAt(STATE, 7.0)).toBeNull();
    expect(gapAt(STATE, 1.0)).toBe(0);
    expect(gapAt(STATE, 14.5)).toBe(1);
    expect(gapAt(STATE, 19.9)).toBe(2);
  });

  it("lists every boundary in order and reports what the server would refuse", () => {
    expect(boundaries(STATE)).toEqual([STATE.kept[0].start, STATE.kept[0].end, STATE.kept[1].start, STATE.kept[1].end]);
    expect(problems(STATE, RULES)).toEqual([]);
    const broken: EditState = { kept: [STATE.kept[1], STATE.kept[0]] };
    expect(problems(broken, RULES)).toHaveLength(1);
  });
});

describe("undo and redo", () => {
  it("walks back and forth, and a no-op commits nothing", () => {
    let history = historyOf(STATE);
    history = commit(history, "togli", removeRange(STATE, 1)!);
    history = commit(history, "niente", history.present);
    expect(history.past).toHaveLength(1);
    expect(history.present.kept).toHaveLength(1);
    history = undo(history);
    expect(history.present).toEqual(STATE);
    expect(history.future[0].label).toBe("togli");
    history = redo(history);
    expect(history.present.kept).toHaveLength(1);
    expect(undo(historyOf(STATE))).toEqual(historyOf(STATE));
  });

  it("a new command after undo forgets the redo branch", () => {
    let history = commit(historyOf(STATE), "togli", removeRange(STATE, 1)!);
    history = undo(history);
    history = commit(history, "dividi", splitRange(STATE, WORDS, 1, 23, RULES)!);
    expect(history.future).toEqual([]);
    expect(history.present.kept).toHaveLength(3);
  });
});

describe("a free edge (⌘-drag)", () => {
  it("stops inside a word and keeps that word in the range", () => {
    const word = WORDS[9];
    const inside = word.t + 0.1;
    const next = moveBoundary(STATE, WORDS, 0, "start", inside, RULES, { free: true })!;
    expect(next.kept[0].first).toBe(9);
    expect(next.kept[0].start).toBeCloseTo(inside, 6);
    expect(next.kept[0].freeStart).toBe(true);
    expect(toRequest(next)[0]).toMatchObject({ freeStart: true });
  });

  it("leaves at least a frame of the word it cuts into", () => {
    const word = WORDS[20];
    const next = moveBoundary(STATE, WORDS, 0, "end", word.t + 0.01, RULES, { free: true })!;
    expect(next.kept[0].last).toBe(20);
    expect(next.kept[0].end).toBeCloseTo(word.t + RULES.frame, 6);
    expect(next.kept[0].freeEnd).toBe(true);
  });

  it("without ⌘ the same drag stops at the word's edge and the flag goes", () => {
    const free = moveBoundary(STATE, WORDS, 0, "start", WORDS[9].t + 0.1, RULES, { free: true })!;
    const snapped = moveBoundary(free, WORDS, 0, "start", WORDS[9].t + 0.1, RULES)!;
    expect(snapped.kept[0].start).toBeCloseTo(WORDS[9].t, 6);
    expect(snapped.kept[0].freeStart).toBe(false);
  });

  it("is read back as free from a plan", () => {
    const free = moveBoundary(STATE, WORDS, 0, "start", WORDS[9].t + 0.1, RULES, { free: true })!;
    const asPlan = {
      segments: free.kept.map((range, index) => ({
        start: range.start, end: range.end, label: `s${index}`, firstWord: range.first, lastWord: range.last,
      })),
    } as unknown as CutPlan;
    expect(fromPlan(asPlan, WORDS).kept[0].freeStart).toBe(true);
  });
});
