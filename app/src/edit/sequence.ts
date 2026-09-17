/** The edit as a sequence: what the film is, not what the footage was.
 *
 *  The cut is kept as ranges of the source (`model.ts`), because that is what
 *  the engine reasons about and what the server stores. But an editor works in
 *  the time of the finished film: the third clip starts at 0:12 of the video,
 *  not at 1:47 of the take. Everything on screen — the ruler, the playhead,
 *  the clips butted against each other — lives in that time, and this module
 *  is the map between the two.
 */

import type { Word } from "../types";
import { EPSILON, type EditState, type KeptRange } from "./model";

export interface Clip extends KeptRange {
  index: number;
  /** The first words of the clip: what it is, read off the transcript rather
   *  than carried from a plan that the next edit would make stale. */
  label: string;
  /** Seconds into the finished film. */
  outputStart: number;
  outputEnd: number;
  duration: number;
}

/** What was taken out between two clips — nothing in the film's time, a mark
 *  on the timeline that can be clicked and put back. */
export interface Cut {
  /** The gap's index in `model.restoreGap` terms: `n` sits before clip `n`. */
  index: number;
  /** Where it falls in the film. */
  at: number;
  /** How much of the source it swallows. */
  removed: number;
  reason: string;
  detail: string;
  /** The words inside it, for the label and for the inspector. */
  firstWord: number | null;
  lastWord: number | null;
}

export interface Sequence {
  clips: Clip[];
  cuts: Cut[];
  duration: number;
}

export interface CutSource {
  removed: { start: number; end: number; reason: string; detail: string }[];
  /** Word runs the engine kept before anyone edited by hand. */
  engineKept: [number, number][];
  sourceDuration: number;
}

export function build(state: EditState, words: Word[], source: CutSource): Sequence {
  const clips: Clip[] = [];
  let cursor = 0;
  state.kept.forEach((range, index) => {
    const duration = Math.max(0, range.end - range.start);
    clips.push({
      ...range, index, label: labelOf(words, range), duration,
      outputStart: cursor, outputEnd: cursor + duration,
    });
    cursor += duration;
  });

  const engine = engineFlags(source.engineKept, words.length);
  const cuts: Cut[] = [];
  let previousEnd = 0;
  clips.forEach((clip) => {
    if (clip.start > previousEnd + EPSILON) {
      cuts.push(describe(previousEnd, clip.start, clip.index, clip.outputStart, words, engine, source));
    }
    previousEnd = clip.end;
  });
  if (previousEnd < source.sourceDuration - EPSILON) {
    cuts.push(
      describe(previousEnd, source.sourceDuration, clips.length, cursor, words, engine, source),
    );
  }
  return { clips, cuts, duration: cursor };
}

/** Where in the source an instant of the film comes from. */
export function sourceAt(sequence: Sequence, output: number): number {
  if (!sequence.clips.length) return 0;
  for (const clip of sequence.clips) {
    if (output < clip.outputEnd - EPSILON) {
      return clip.start + Math.max(0, output - clip.outputStart);
    }
  }
  const last = sequence.clips[sequence.clips.length - 1];
  return last.end;
}

/** Where in the film an instant of the source lands. Material that was cut
 *  maps to the join it was cut out of, which is where the playhead belongs. */
export function outputAt(sequence: Sequence, source: number): number {
  for (const clip of sequence.clips) {
    if (source < clip.start) return clip.outputStart;
    if (source <= clip.end + EPSILON) return clip.outputStart + (source - clip.start);
  }
  return sequence.duration;
}

export function clipAt(sequence: Sequence, output: number): Clip | null {
  for (const clip of sequence.clips) {
    if (output >= clip.outputStart - EPSILON && output <= clip.outputEnd + EPSILON) return clip;
  }
  return null;
}

/** Every join, in film time: where `[` and `]` jump to. */
export function joins(sequence: Sequence): number[] {
  const points = [0];
  for (const clip of sequence.clips) points.push(clip.outputEnd);
  return points;
}

export function totalRemoved(sequence: Sequence): number {
  return sequence.cuts.reduce((sum, cut) => sum + cut.removed, 0);
}

// ------------------------------------------------------------------ pieces

function labelOf(words: Word[], range: KeptRange): string {
  const said = words.slice(range.first, range.last + 1).map((word) => word.s).join(" ");
  return said.length > 64 ? `${said.slice(0, 63)}…` : said;
}

function engineFlags(kept: [number, number][], total: number): Uint8Array {
  const flags = new Uint8Array(total);
  for (const [first, last] of kept) {
    for (let index = Math.max(0, first); index <= Math.min(total - 1, last); index += 1) {
      flags[index] = 1;
    }
  }
  return flags;
}

/** Why a piece is missing, in the same terms the server will use when it
 *  stores the plan: no words is a pause, words the engine had kept are the
 *  person's own removal, the rest keep the engine's reason. */
function describe(
  start: number,
  end: number,
  index: number,
  at: number,
  words: Word[],
  engine: Uint8Array,
  source: CutSource,
): Cut {
  const inside: number[] = [];
  words.forEach((word, wordIndex) => {
    if (word.t >= start - EPSILON && word.end <= end + EPSILON) inside.push(wordIndex);
  });
  const removed = end - start;
  const base = {
    index,
    at,
    removed,
    firstWord: inside.length ? inside[0] : null,
    lastWord: inside.length ? inside[inside.length - 1] : null,
  };

  if (!inside.length) {
    if (start <= EPSILON) return { ...base, reason: "lead-in", detail: "silenzio prima della prima battuta" };
    if (end >= source.sourceDuration - EPSILON) {
      return { ...base, reason: "lead-out", detail: "silenzio dopo l'ultima battuta" };
    }
    return { ...base, reason: "silence", detail: `pausa di ${removed.toFixed(2)}s` };
  }

  const said = inside.map((wordIndex) => words[wordIndex].s).join(" ");
  if (inside.some((wordIndex) => engine[wordIndex])) {
    return { ...base, reason: "manual", detail: said };
  }
  const known = source.removed.find(
    (item) =>
      !["silence", "lead-in", "lead-out"].includes(item.reason) &&
      item.start < end - EPSILON &&
      item.end > start + EPSILON,
  );
  return { ...base, reason: known?.reason ?? "retake", detail: known?.detail ?? said };
}
