/** The cut as it is on the timeline, and every way a person changes it.
 *
 *  One value: the ordered list of kept word ranges, each with the seconds its
 *  edges sit at. Every command is a pure function from a state to a new state
 *  (or `null` when what was asked is not allowed), which is what makes undo a
 *  list of old states, the preview a function of the current one, and the
 *  request to the server a straight serialisation. The server realises the
 *  same list with the same rules (`skyground/analysis/manual.py`); what is
 *  checked here is checked there again, so the client can never save a cut
 *  the engine's invariants refuse.
 */

import type { CutPlan, Policy, Word } from "../types";

export interface KeptRange {
  /** Indices into the prepared transcript's words, both inclusive. */
  first: number;
  last: number;
  /** Seconds in the source. Always inside the gap around the words. */
  start: number;
  end: number;
}

export interface EditState {
  kept: readonly KeptRange[];
}

export interface Rules {
  leadIn: number;
  leadOut: number;
  minSegment: number;
  duration: number;
}

/** Word timings are milliseconds; a boundary may sit exactly on an edge. */
export const EPSILON = 0.0015;

export function rulesOf(plan: CutPlan, duration: number): Rules {
  const policy = plan.policy ?? ({} as Policy);
  return {
    leadIn: Number(policy.lead_in ?? 0.12),
    leadOut: Number(policy.lead_out ?? 0.28),
    minSegment: Number(policy.min_segment ?? 0.35),
    duration,
  };
}

// ------------------------------------------------------------------ in/out

export function fromPlan(plan: CutPlan, words: Word[]): EditState {
  const kept: KeptRange[] = [];
  for (const segment of plan.segments) {
    let first = segment.firstWord;
    let last = segment.lastWord;
    if (first === null || last === null) {
      const inside = wordsInside(words, segment.start, segment.end);
      if (!inside.length) continue;
      first = inside[0];
      last = inside[inside.length - 1];
    }
    kept.push({ first, last, start: segment.start, end: segment.end });
  }
  return { kept };
}

export function toRequest(state: EditState) {
  return state.kept.map((range) => ({
    first: range.first,
    last: range.last,
    start: round3(range.start),
    end: round3(range.end),
  }));
}

/** What the player skips between: the kept ranges as plain seconds. */
export function segmentsOf(state: EditState): { start: number; end: number }[] {
  return state.kept.map((range) => ({ start: range.start, end: range.end }));
}

export function sameCut(a: EditState, b: EditState): boolean {
  if (a.kept.length !== b.kept.length) return false;
  return a.kept.every((range, index) => {
    const other = b.kept[index];
    return (
      range.first === other.first &&
      range.last === other.last &&
      Math.abs(range.start - other.start) < EPSILON &&
      Math.abs(range.end - other.end) < EPSILON
    );
  });
}

// ---------------------------------------------------------------- commands

/** Move one edge of a range to `seconds`. Words the edge passes over leave or
 *  join the range; the edge itself is then clamped into the gap around the
 *  range's outermost word, so it can never sit inside speech. */
export function moveBoundary(
  state: EditState,
  words: Word[],
  index: number,
  side: "start" | "end",
  seconds: number,
  rules: Rules,
): EditState | null {
  const range = state.kept[index];
  if (!range) return null;
  const previous = state.kept[index - 1];
  const next = state.kept[index + 1];
  let updated: KeptRange;

  // Inside a word the edge stops at that word's outer side: a word leaves the
  // range only once the edge has been dragged all the way past it.
  const inside = wordAround(words, seconds);
  if (inside !== null) seconds = side === "start" ? words[inside].t : words[inside].end;

  if (side === "start") {
    const lowest = previous ? previous.last + 1 : 0;
    let first = firstWordFrom(words, seconds);
    first = Math.max(lowest, Math.min(first, range.last));
    const lo = Math.max(first > 0 ? words[first - 1].end : 0, previous ? previous.end : 0, 0);
    const hi = words[first].t;
    updated = { ...range, first, start: clamp(seconds, lo, hi) };
  } else {
    const highest = next ? next.first - 1 : words.length - 1;
    let last = lastWordUpTo(words, seconds);
    last = Math.min(highest, Math.max(last, range.first));
    const lo = words[last].end;
    const hi = Math.min(last + 1 < words.length ? words[last + 1].t : rules.duration, next ? next.start : rules.duration, rules.duration);
    updated = { ...range, last, end: clamp(seconds, lo, hi) };
  }
  if (updated.end - updated.start < rules.minSegment - EPSILON) return null;
  return replaceAt(state, index, [updated]);
}

/** Take a whole range out of the edit. */
export function removeRange(state: EditState, index: number): EditState | null {
  if (!state.kept[index]) return null;
  return replaceAt(state, index, []);
}

/** Put a gap back: the two ranges around it (and any words inside it) become
 *  one. Gap 0 is the air before the first range, gap n the air after the last. */
export function restoreGap(
  state: EditState,
  words: Word[],
  gap: number,
  rules: Rules,
): EditState | null {
  const before = state.kept[gap - 1];
  const after = state.kept[gap];
  if (!before && !after) return null;
  if (!before) {
    // Everything from the start of the source up to the first range.
    const merged: KeptRange = { ...after, first: 0, start: 0 };
    return replaceAt(state, gap, [merged]);
  }
  if (!after) {
    const merged: KeptRange = { ...before, last: words.length - 1, end: rules.duration };
    return replaceAt(state, gap - 1, [merged]);
  }
  const merged: KeptRange = { first: before.first, last: after.last, start: before.start, end: after.end };
  return { kept: [...state.kept.slice(0, gap - 1), merged, ...state.kept.slice(gap + 1)] };
}

/** Cut a range in two after `afterWord`. The two halves get the policy's air
 *  on either side of the split, as far as the gap between the words allows. */
export function splitRange(
  state: EditState,
  words: Word[],
  index: number,
  afterWord: number,
  rules: Rules,
): EditState | null {
  const range = state.kept[index];
  if (!range || afterWord < range.first || afterWord >= range.last) return null;
  const gapStart = words[afterWord].end;
  const gapEnd = words[afterWord + 1].t;
  let leftEnd = Math.min(gapStart + rules.leadOut, gapEnd);
  let rightStart = Math.max(gapEnd - rules.leadIn, gapStart);
  if (rightStart < leftEnd) {
    const middle = (gapStart + gapEnd) / 2;
    leftEnd = middle;
    rightStart = middle;
  }
  const left: KeptRange = { first: range.first, last: afterWord, start: range.start, end: leftEnd };
  const right: KeptRange = { first: afterWord + 1, last: range.last, start: rightStart, end: range.end };
  if (left.end - left.start < rules.minSegment - EPSILON) return null;
  if (right.end - right.start < rules.minSegment - EPSILON) return null;
  return replaceAt(state, index, [left, right]);
}

/** Bring the edge of the range under `seconds` to `seconds`: the I and O keys. */
export function trimToPlayhead(
  state: EditState,
  words: Word[],
  seconds: number,
  side: "start" | "end",
  rules: Rules,
): EditState | null {
  const index = rangeAt(state, seconds);
  if (index === null) return null;
  return moveBoundary(state, words, index, side, seconds, rules);
}

/** Which range contains `seconds`, or null in a gap. */
export function rangeAt(state: EditState, seconds: number): number | null {
  const index = state.kept.findIndex((range) => seconds >= range.start - EPSILON && seconds <= range.end + EPSILON);
  return index >= 0 ? index : null;
}

/** Which gap contains `seconds`: 0 before the first range, n after the last. */
export function gapAt(state: EditState, seconds: number): number | null {
  if (rangeAt(state, seconds) !== null) return null;
  let gap = 0;
  for (const range of state.kept) {
    if (seconds > range.end) gap += 1;
  }
  return gap;
}

/** Every boundary, in order: where `[` and `]` jump to. */
export function boundaries(state: EditState): number[] {
  const points: number[] = [];
  for (const range of state.kept) points.push(range.start, range.end);
  return points;
}

/** Anything the server would refuse, in the person's words. */
export function problems(state: EditState, rules: Rules): string[] {
  const found: string[] = [];
  let previous: KeptRange | null = null;
  state.kept.forEach((range, index) => {
    if (range.end - range.start < rules.minSegment - EPSILON) {
      found.push(`il pezzo ${index + 1} dura ${(range.end - range.start).toFixed(2)}s, minimo ${rules.minSegment.toFixed(2)}s`);
    }
    if (previous && (range.first <= previous.last || range.start < previous.end - EPSILON)) {
      found.push(`il pezzo ${index + 1} comincia prima della fine del precedente`);
    }
    previous = range;
  });
  return found;
}

// ----------------------------------------------------------------- history

export interface HistoryEntry {
  label: string;
  state: EditState;
}

export interface History {
  past: HistoryEntry[];
  present: EditState;
  future: HistoryEntry[];
  /** What the last command was called, for "Annulla: sposta confine". */
  label: string;
}

export const HISTORY_LIMIT = 200;

export function historyOf(state: EditState): History {
  return { past: [], present: state, future: [], label: "" };
}

export function commit(history: History, label: string, state: EditState): History {
  if (sameCut(history.present, state)) return history;
  const past = [...history.past, { label: history.label, state: history.present }].slice(-HISTORY_LIMIT);
  return { past, present: state, future: [], label };
}

export function undo(history: History): History {
  const entry = history.past[history.past.length - 1];
  if (!entry) return history;
  return {
    past: history.past.slice(0, -1),
    present: entry.state,
    future: [{ label: history.label, state: history.present }, ...history.future],
    label: entry.label,
  };
}

export function redo(history: History): History {
  const entry = history.future[0];
  if (!entry) return history;
  return {
    past: [...history.past, { label: history.label, state: history.present }],
    present: entry.state,
    future: history.future.slice(1),
    label: entry.label,
  };
}

// ----------------------------------------------------------------- helpers

function replaceAt(state: EditState, index: number, ranges: KeptRange[]): EditState {
  return { kept: [...state.kept.slice(0, index), ...ranges, ...state.kept.slice(index + 1)] };
}

/** The first word that starts at or after `seconds`. */
export function firstWordFrom(words: Word[], seconds: number): number {
  let lo = 0;
  let hi = words.length - 1;
  let found = words.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid].t >= seconds - EPSILON) {
      found = mid;
      hi = mid - 1;
    } else lo = mid + 1;
  }
  return found;
}

/** The last word that ends at or before `seconds`. */
export function lastWordUpTo(words: Word[], seconds: number): number {
  let lo = 0;
  let hi = words.length - 1;
  let found = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid].end <= seconds + EPSILON) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/** The word `seconds` falls strictly inside, or null on an edge or in a gap. */
export function wordAround(words: Word[], seconds: number): number | null {
  let lo = 0;
  let hi = words.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const word = words[mid];
    if (seconds < word.t + EPSILON) hi = mid - 1;
    else if (seconds > word.end - EPSILON) lo = mid + 1;
    else return mid;
  }
  return null;
}

export function wordsInside(words: Word[], start: number, end: number): number[] {
  const inside: number[] = [];
  words.forEach((word, index) => {
    if (word.t >= start - EPSILON && word.end <= end + EPSILON) inside.push(index);
  });
  return inside;
}

export function clamp(value: number, lo: number, hi: number): number {
  return Math.min(Math.max(value, lo), Math.max(lo, hi));
}

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}
