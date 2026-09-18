/** Where a dragged edge lands.
 *
 *  Two rules, one soft and one hard. The magnet: within a few pixels of a
 *  word edge or a measured silence edge, the edge takes it — those are the
 *  places a cut belongs. The clamp: an edge never rests inside a word, magnet
 *  or not; dropped in one, it goes to the nearer side of that word. Inside a
 *  silence, where no word decides, it lands on a whole frame.
 */

import type { Word } from "../types";
import { EPSILON, wordAround } from "./model";

export { wordAround };

export interface Silence {
  start: number;
  end: number;
}

export interface SnapContext {
  words: Word[];
  silences: Silence[];
  fps: number;
  duration: number;
}

/** Sorted candidate times: every word edge and every silence edge. */
export function candidates(words: Word[], silences: Silence[]): number[] {
  const points: number[] = [];
  for (const word of words) points.push(word.t, word.end);
  for (const silence of silences) points.push(silence.start, silence.end);
  points.sort((a, b) => a - b);
  return points.filter((point, index) => index === 0 || point - points[index - 1] > EPSILON);
}

export function quantiseToFrame(seconds: number, fps: number): number {
  return Math.round(seconds * fps) / fps;
}

/**
 * @param seconds where the pointer is
 * @param pxPerSec the timeline's scale, so the magnet is a distance on screen
 * @param magnetPx how close, in pixels, a candidate has to be to take the edge
 */
export function snap(
  seconds: number,
  context: SnapContext,
  sortedCandidates: number[],
  pxPerSec: number,
  magnetPx = 8,
): number {
  return snapped(seconds, context, sortedCandidates, pxPerSec, magnetPx).at;
}

/** The same, saying whether the magnet took the edge: the timeline shows it. */
export function snapped(
  seconds: number,
  context: SnapContext,
  sortedCandidates: number[],
  pxPerSec: number,
  magnetPx = 8,
): { at: number; magnet: boolean } {
  let at = Math.min(Math.max(seconds, 0), context.duration);
  const reach = magnetPx / Math.max(pxPerSec, 1e-6);
  const nearest = nearestOf(sortedCandidates, at);
  const magnet = nearest !== null && Math.abs(nearest - at) <= reach;
  if (magnet) at = nearest;

  const inside = wordAround(context.words, at);
  if (inside !== null) {
    const word = context.words[inside];
    at = at - word.t < word.end - at ? word.t : word.end;
  } else if (nearest === null || Math.abs(nearest - at) > EPSILON) {
    // In the open: whole frames, unless that would push it into a word.
    const framed = quantiseToFrame(at, context.fps);
    if (wordAround(context.words, framed) === null) at = framed;
  }
  return { at: Math.min(Math.max(at, 0), context.duration), magnet };
}

function nearestOf(sorted: number[], value: number): number | null {
  if (!sorted.length) return null;
  let lo = 0;
  let hi = sorted.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] < value) lo = mid + 1;
    else hi = mid;
  }
  const after = sorted[lo];
  const before = lo > 0 ? sorted[lo - 1] : after;
  return Math.abs(before - value) <= Math.abs(after - value) ? before : after;
}
