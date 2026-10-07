import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import { moveBoundary, splitRange, wordAround, type EditState, type Rules } from "../edit/model";
import { candidates, quantiseToFrame, snapped, type Silence } from "../edit/snap";
import {
  build,
  clipAt,
  outputAt,
  sourceAt,
  type Clip,
  type Cut,
  type CutSource,
  type Sequence,
} from "../edit/sequence";
import type { MediaInfo, Question, Word } from "../types";
import { loadPeaks, peakBetween, type Peaks } from "./timeline/peaks";
import { loadSheets, locate, type Sheets } from "./timeline/thumbs";
import { formatTimecode, tickStep } from "./timeline/time";

export type Selection = { kind: "clip"; index: number } | { kind: "cut"; index: number };

export interface TimelineHandle {
  /** Zoom around the pointer when it is on the timeline, else the playhead. */
  zoomBy: (factor: number) => void;
  fit: () => void;
  reveal: (output: number) => void;
  redraw: () => void;
}

/** A trim in flight, for the two-up view in the player: the frame either side
 *  of the join being made, in source seconds (null: the film's edge). */
export interface TrimInfo {
  side: "start" | "end";
  left: number | null;
  right: number | null;
  /** How far the edge has moved, seconds, signed as the film sees it. */
  delta: number;
  /** The clip's length as it would be. */
  duration: number;
  /** The edge sits inside a word (⌘-drag). */
  inWord: boolean;
}

interface Props {
  state: EditState;
  words: Word[];
  silences: Silence[];
  rules: Rules;
  source: CutSource;
  media: MediaInfo | undefined;
  fps: number;
  questions: Question[];
  selection: Selection | null;
  /** Film time, mutated by the player at frame rate: read here, never a prop. */
  playhead: { current: number };
  playing: boolean;
  snapping: boolean;
  /** CSS pixels: the panel above sets it with its divider. */
  height: number;
  onScrub: (output: number) => void;
  onSelect: (selection: Selection | null) => void;
  onCommit: (label: string, state: EditState) => void;
  onPreview: (preview: { state: EditState; sequence: Sequence } | null) => void;
  onTrim: (trim: TrimInfo | null) => void;
  onZoom: (pxPerSec: number, fit: number) => void;
}

// ------------------------------------------------------------------ the look

const C = {
  bg: "#0c0c0f",
  head: "#111115",
  line: "#222229",
  ruler: "#8a877f",
  rulerLine: "#2c2c33",
  clip: "#1c1e25",
  clipTop: "#262932",
  audio: "#12161a",
  audioClip: "#16201a",
  edge: "rgba(255,255,255,0.07)",
  select: "#f2f1ee",
  accent: "#d8ff3e",
  wave: "rgba(152,214,120,0.78)",
  waveDim: "rgba(152,214,120,0.35)",
  ink: "#f2f1ee",
  muted: "#8d8a83",
  playhead: "#ffffff",
  hover: "rgba(255,255,255,0.32)",
  question: "#ff6b3d",
  questionDone: "#4a6b3c",
  split: "#ff5a4f",
};

const REASON: Record<string, { dot: string; label: string }> = {
  silence: { dot: "#6b6b72", label: "pausa" },
  "lead-in": { dot: "#6b6b72", label: "testa" },
  "lead-out": { dot: "#6b6b72", label: "coda" },
  filler: { dot: "#bfa246", label: "intercalare" },
  retake: { dot: "#d07a4e", label: "ripetizione" },
  manual: { dot: "#8f84e8", label: "tolto a mano" },
};

/** The track headers: V1 over A1, as every editor lays them. */
const HEAD_W = 44;
/** A gutter at either end of the axis, so zero and the end are not cut in half. */
const PAD = 12;
const RULER_H = 28;
const MARK_H = 14;
const CLIP_TOP = RULER_H + MARK_H;
const LANE_GAP = 3;
const AUDIO_H = 42;
const BOTTOM = 16;
const HANDLE_W = 6;
const GRAB = 8;
const MAGNET_PX = 8;
const MIN_PX_PER_SEC = 4;
const MAX_PX_PER_SEC = 900;
/** Near the edges a drag scrolls the view, faster the closer it gets. */
const EDGE_SCROLL = 36;

interface Geometry {
  height: number;
  videoH: number;
  audioTop: number;
  bottom: number;
}

function geometryOf(height: number): Geometry {
  const videoH = Math.max(40, height - CLIP_TOP - LANE_GAP - AUDIO_H - BOTTOM);
  const audioTop = CLIP_TOP + videoH + LANE_GAP;
  return { height, videoH, audioTop, bottom: audioTop + AUDIO_H };
}

type Trim = {
  kind: "trim";
  clip: Clip;
  /** The film as it was when the hand went down: the layout while dragging. */
  base: Sequence;
  side: "start" | "end";
  next: EditState | null;
  moved: boolean;
  free: boolean;
  magnet: boolean;
  /** Where the edge is now, source seconds. */
  edge: number;
};

type Drag = { kind: "scrub" } | Trim | null;

/** The film on one axis: the clips butted against each other, the joins where
 *  something was taken out, and the handles that move them.
 *
 *  It draws itself on a single canvas from a mutable scene, inside one
 *  animation frame, so the playhead can run at the video's frame rate without
 *  React re-rendering anything. Everything is a function of `x(output)`, and
 *  only the seconds on screen are ever drawn.
 *
 *  A trim works the way Premiere and CapCut do it: while the hand is down the
 *  edge follows the pointer and nothing else moves — a shortened clip leaves
 *  the part it gives up as a ghost, a lengthened one lies over its neighbour —
 *  and the film closes up only on release.
 */
export const Timeline = forwardRef<TimelineHandle, Props>(function Timeline(props, handle) {
  const {
    state, words, silences, rules, source, media, fps, questions, selection,
    playhead, playing, snapping, height, onScrub, onSelect, onCommit, onPreview, onTrim, onZoom,
  } = props;

  const canvas = useRef<HTMLCanvasElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag>(null);
  const hover = useRef<{ clip: number | null; cut: number | null; handle: "start" | "end" | null; px: number | null }>({
    clip: null, cut: null, handle: null, px: null,
  });
  /** The last pointer position of a drag, replayed while the view scrolls. */
  const pointer = useRef<{ px: number; free: boolean } | null>(null);
  const view = useRef({ pxPerSec: 0, start: 0, width: 1000 });
  const peaks = useRef<Peaks | null>(null);
  const sheets = useRef<Sheets | null>(null);
  const dirty = useRef(true);
  const scene = useRef({ sequence: null as Sequence | null });
  const geometry = useMemo(() => geometryOf(height), [height]);

  const sequence = useMemo(() => build(state, words, source), [state, words, source]);
  scene.current.sequence = sequence;
  const snapPoints = useMemo(() => candidates(words, silences), [words, silences]);
  const snapContext = useMemo(
    () => ({ words, silences, fps, duration: source.sourceDuration }),
    [words, silences, fps, source.sourceDuration],
  );

  const invalidate = useCallback(() => { dirty.current = true; }, []);
  const shown = () => scene.current.sequence ?? sequence;
  /** The layout while dragging is the film from before the drag. */
  const laidOut = () => (drag.current?.kind === "trim" ? drag.current.base : shown());

  const origin = HEAD_W + PAD;
  const span = () => Math.max(120, view.current.width - origin - PAD);
  const fitScale = () => Math.max(MIN_PX_PER_SEC, span() / Math.max(laidOut().duration, 0.5));
  const scale = () => view.current.pxPerSec || fitScale();
  const x = (output: number) => origin + (output - view.current.start) * scale();
  const timeAt = (px: number) => view.current.start + (px - origin) / scale();

  const clampStart = (start: number, px: number) =>
    Math.max(0, Math.min(start, Math.max(0, laidOut().duration - span() / px)));

  // --------------------------------------------------------------- resources

  useEffect(() => {
    const element = wrap.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      view.current.width = Math.max(240, entry.contentRect.width);
      invalidate();
    });
    observer.observe(element);
    view.current.width = Math.max(240, element.clientWidth);
    return () => observer.disconnect();
  }, [invalidate]);

  useEffect(() => {
    if (!media?.ready || !media.peaks) return;
    let cancelled = false;
    void loadPeaks(media.peaks, media.peaksRate ?? 100)
      .then((loaded) => { if (!cancelled) { peaks.current = loaded; invalidate(); } })
      .catch(() => { /* the waveform helps, it is not required */ });
    return () => { cancelled = true; };
  }, [media?.ready, media?.peaks, media?.peaksRate, invalidate]);

  useEffect(() => {
    if (!media?.ready || !media.thumbs?.urls.length) return;
    sheets.current = loadSheets(media.thumbs, invalidate);
    invalidate();
  }, [media?.ready, media?.thumbs, invalidate]);

  useEffect(invalidate, [sequence, selection, questions, height, invalidate]);

  // ------------------------------------------------------------------ zoom

  /** Zoom keeping the instant under `atPx` exactly where it is. */
  const zoomAround = useCallback((factor: number, atPx: number) => {
    const before = timeAt(atPx);
    const fit = fitScale();
    const next = Math.max(fit, Math.min(MAX_PX_PER_SEC, scale() * factor));
    view.current.pxPerSec = next;
    view.current.start = clampStart(before - (atPx - origin) / next, next);
    onZoom(next, fit);
    invalidate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invalidate, onZoom]);

  /** Where a zoom from the toolbar or the keyboard should hold still: the
   *  pointer if it is on the timeline, else the playhead if it is in view. */
  const zoomAnchor = () => {
    if (hover.current.px !== null) return hover.current.px;
    const at = x(playhead.current);
    if (at >= origin && at <= view.current.width - PAD) return at;
    return origin + span() / 2;
  };

  useImperativeHandle(handle, () => ({
    zoomBy: (factor) => zoomAround(factor, zoomAnchor()),
    fit: () => {
      view.current.pxPerSec = 0;
      view.current.start = 0;
      onZoom(fitScale(), fitScale());
      invalidate();
    },
    reveal: (output) => {
      const px = x(output);
      if (px >= origin + 30 && px <= view.current.width - 40) return;
      view.current.start = clampStart(output - span() / scale() / 3, scale());
      invalidate();
    },
    redraw: invalidate,
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [zoomAround, invalidate, onZoom]);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = element.getBoundingClientRect();
      if (event.ctrlKey || event.metaKey) {
        zoomAround(Math.exp(-event.deltaY * 0.0125), Math.max(origin, event.clientX - box.left));
        return;
      }
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      view.current.start = clampStart(view.current.start + delta / scale(), scale());
      invalidate();
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zoomAround, invalidate]);

  // --------------------------------------------------------------- pointer

  const hit = (px: number, py: number) => {
    const current = shown();
    const found = { clip: null as number | null, cut: null as number | null, handle: null as "start" | "end" | null };
    if (px < HEAD_W) return found;
    if (py >= RULER_H && py < CLIP_TOP) {
      const cut = current.cuts.find((item) => Math.abs(x(item.at) - px) <= 7);
      if (cut) found.cut = cut.index;
      return found;
    }
    if (py < CLIP_TOP || py > geometry.bottom) return found;
    // The selected clip owns its edges. Without this the clip before it wins
    // the point — the grab zones touch — and its left handle cannot be taken.
    const chosen = selection?.kind === "clip" ? current.clips[selection.index] : undefined;
    const onChosen = chosen
      && px >= x(chosen.outputStart) - GRAB && px <= x(chosen.outputEnd) + GRAB;
    const clip = onChosen
      ? chosen
      : current.clips.find((item) => px >= x(item.outputStart) && px <= x(item.outputEnd));
    if (!clip) return found;
    found.clip = clip.index;
    const left = x(clip.outputStart);
    const right = x(clip.outputEnd);
    // Grab zones reach a little inside the clip and a little out of it, and
    // never more than a third of a narrow clip, which must stay clickable.
    const inward = Math.min(GRAB + 2, (right - left) / 3);
    if (px >= left - GRAB && px <= left + inward) found.handle = "start";
    else if (px >= right - inward && px <= right + GRAB) found.handle = "end";
    return found;
  };

  const local = (event: React.PointerEvent | React.MouseEvent) => {
    const box = canvas.current!.getBoundingClientRect();
    return { px: event.clientX - box.left, py: event.clientY - box.top };
  };

  const trimTo = (current: Trim, px: number, free: boolean) => {
    const { clip, side, base } = current;
    const wantedOutput = timeAt(px);
    const anchor = side === "start" ? clip.outputStart : clip.outputEnd;
    const wantedSource = (side === "start" ? clip.start : clip.end) + (wantedOutput - anchor);
    let target = wantedSource;
    let magnet = false;
    if (free) {
      target = quantiseToFrame(wantedSource, fps);
    } else if (snapping) {
      const result = snapped(wantedSource, snapContext, snapPoints, scale(), MAGNET_PX);
      target = result.at;
      magnet = result.magnet;
      // The playhead is a place a cut belongs too, when it is on this clip.
      const at = playhead.current;
      const under = clipAt(base, at);
      if (under && under.index === clip.index && Math.abs(x(at) - px) <= MAGNET_PX) {
        target = sourceAt(base, at);
        magnet = true;
      }
    }
    current.free = free;
    const next = moveBoundary(state, words, clip.index, side, target, rules, { free });
    if (!next) return;
    const range = next.kept[clip.index];
    current.next = next;
    current.moved = true;
    current.magnet = magnet;
    current.edge = side === "start" ? range.start : range.end;
    const preview = build(next, words, source);
    onPreview({ state: next, sequence: preview });

    const frame = 1 / fps;
    const before = base.clips[clip.index - 1];
    const after = base.clips[clip.index + 1];
    onTrim({
      side,
      left: side === "start" ? (before ? before.end - frame : null) : range.end - frame,
      right: side === "start" ? range.start : after ? after.start : null,
      delta: side === "start" ? clip.start - range.start : range.end - clip.end,
      duration: range.end - range.start,
      inWord: wordAround(words, current.edge) !== null,
    });
    invalidate();
  };

  const startTrim = (clip: Clip, side: "start" | "end") => {
    drag.current = {
      kind: "trim", clip, base: shown(), side, next: null, moved: false,
      free: false, magnet: false, edge: side === "start" ? clip.start : clip.end,
    };
    const before = shown().clips[clip.index - 1];
    const after = shown().clips[clip.index + 1];
    // Either side of the join: the last frame shown before it, the first after.
    onTrim({
      side,
      left: side === "start" ? (before ? before.end - 1 / fps : null) : clip.end - 1 / fps,
      right: side === "start" ? clip.start : after ? after.start : null,
      delta: 0,
      duration: clip.duration,
      inWord: false,
    });
    invalidate();
  };

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    const { px, py } = local(event);
    if (px < HEAD_W) return;
    canvas.current?.setPointerCapture(event.pointerId);
    const found = hit(px, py);
    pointer.current = { px, free: event.metaKey };

    if (found.cut !== null) {
      onSelect({ kind: "cut", index: found.cut });
      const cut = shown().cuts.find((item) => item.index === found.cut);
      if (cut) onScrub(cut.at);
      return;
    }
    if (found.clip !== null) {
      onSelect({ kind: "clip", index: found.clip });
      if (found.handle) {
        startTrim(shown().clips[found.clip], found.handle);
        return;
      }
    } else if (py > geometry.bottom) {
      onSelect(null);
    }
    drag.current = { kind: "scrub" };
    onScrub(Math.max(0, Math.min(shown().duration, timeAt(px))));
  };

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const { px, py } = local(event);
    const current = drag.current;

    if (!current) {
      const found = hit(px, py);
      const inside = px >= HEAD_W;
      const changed =
        found.clip !== hover.current.clip || found.cut !== hover.current.cut ||
        found.handle !== hover.current.handle || hover.current.px !== (inside ? px : null);
      hover.current = { clip: found.clip, cut: found.cut, handle: found.handle, px: inside ? px : null };
      if (canvas.current) {
        canvas.current.style.cursor = found.handle
          ? (event.metaKey ? "ew-resize" : "col-resize")
          : found.cut !== null ? "pointer" : "default";
      }
      if (changed) invalidate();
      return;
    }

    pointer.current = { px, free: event.metaKey };
    if (current.kind === "scrub") {
      onScrub(Math.max(0, Math.min(shown().duration, timeAt(px))));
      return;
    }
    trimTo(current, px, event.metaKey);
  };

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const current = drag.current;
    drag.current = null;
    pointer.current = null;
    canvas.current?.releasePointerCapture(event.pointerId);
    if (current?.kind === "trim") {
      onPreview(null);
      onTrim(null);
      if (current.moved && current.next) {
        onCommit(current.side === "start" ? "taglia l'inizio" : "taglia la fine", current.next);
      }
      invalidate();
    }
  };

  const onDoubleClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const { px, py } = local(event);
    if (px < HEAD_W || py < CLIP_TOP || py > geometry.bottom) return;
    const current = shown();
    const clip = clipAt(current, timeAt(px));
    if (!clip) return;
    const at = sourceAt(current, timeAt(px));
    let after = clip.first;
    for (let index = clip.first; index < clip.last; index += 1) {
      if (words[index].end <= at) after = index;
    }
    const split = splitRange(state, words, clip.index, after, rules);
    if (split) onCommit("dividi", split);
  };

  // ------------------------------------------------------------------ paint

  useEffect(() => {
    let frame = 0;
    const loop = () => {
      frame = requestAnimationFrame(loop);
      // A drag near either edge scrolls the view and replays the last pointer
      // position against it, so the edge keeps going while the hand is still.
      const held = pointer.current;
      if (drag.current && held) {
        const left = origin + EDGE_SCROLL;
        const right = view.current.width - EDGE_SCROLL;
        const push = held.px < left ? held.px - left : held.px > right ? held.px - right : 0;
        if (push !== 0) {
          const before = view.current.start;
          view.current.start = clampStart(before + (push * 0.35) / scale(), scale());
          if (view.current.start !== before) {
            if (drag.current.kind === "trim") trimTo(drag.current, held.px, held.free);
            else onScrub(Math.max(0, Math.min(shown().duration, timeAt(held.px))));
            dirty.current = true;
          }
        }
      }
      const active = playing || drag.current !== null;
      if (!dirty.current && !active) return;
      dirty.current = false;
      const element = canvas.current;
      if (!element) return;
      const trim = drag.current?.kind === "trim" ? drag.current : null;
      paint(element, {
        view: view.current,
        geometry,
        sequence: laidOut(),
        words,
        questions,
        selection,
        hover: hover.current,
        playhead: playhead.current,
        peaks: peaks.current,
        sheets: sheets.current,
        trim: trim && {
          clip: trim.clip.index,
          side: trim.side,
          range: trim.next?.kept[trim.clip.index] ?? trim.clip,
          magnet: trim.magnet,
          free: trim.free,
        },
        fps,
      });
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [words, questions, selection, playing, sequence, geometry, fps]);

  return (
    <div className="tl" ref={wrap}>
      <canvas
        ref={canvas}
        height={height}
        style={{ width: "100%", height }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={() => {
          if (drag.current) return;
          hover.current = { clip: null, cut: null, handle: null, px: null };
          invalidate();
        }}
        onDoubleClick={onDoubleClick}
        aria-label="Timeline del montaggio"
      />
    </div>
  );
});

// ---------------------------------------------------------------- painting

interface Scene {
  view: { pxPerSec: number; start: number; width: number };
  geometry: Geometry;
  sequence: Sequence;
  words: Word[];
  questions: Question[];
  selection: Selection | null;
  hover: { clip: number | null; cut: number | null; handle: "start" | "end" | null; px: number | null };
  playhead: number;
  peaks: Peaks | null;
  sheets: Sheets | null;
  trim: {
    clip: number;
    side: "start" | "end";
    range: { start: number; end: number };
    magnet: boolean;
    free: boolean;
  } | null;
  fps: number;
}

/** One piece of footage on screen: where it is, and which second it starts at. */
interface Piece {
  left: number;
  right: number;
  source: number;
  /** Where it ends in the source: exact, not read back off the pixels. */
  sourceEnd: number;
}

type X = (t: number) => number;

function paint(element: HTMLCanvasElement, scene: Scene) {
  const dpr = window.devicePixelRatio || 1;
  const { width } = scene.view;
  const { height } = scene.geometry;
  if (element.width !== Math.round(width * dpr) || element.height !== Math.round(height * dpr)) {
    element.width = Math.round(width * dpr);
    element.height = Math.round(height * dpr);
  }
  const ctx = element.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, width, height);

  const origin = HEAD_W + PAD;
  const px = scene.view.pxPerSec
    || Math.max(MIN_PX_PER_SEC, Math.max(120, width - origin - PAD) / Math.max(scene.sequence.duration, 0.5));
  const x: X = (output) => origin + (output - scene.view.start) * px;
  const viewEnd = scene.view.start + (width - origin) / px;

  ctx.save();
  ctx.beginPath();
  ctx.rect(HEAD_W, 0, width - HEAD_W, height);
  ctx.clip();
  drawRuler(ctx, scene, x, px, viewEnd, width);
  drawLanes(ctx, scene, width);
  drawClips(ctx, scene, x, px, viewEnd);
  if (!scene.trim) drawCuts(ctx, scene, x, viewEnd);
  drawQuestions(ctx, scene, x, viewEnd);
  if (scene.trim) drawTrimEdge(ctx, scene, x, px);
  else drawHover(ctx, scene, x, px);
  drawPlayhead(ctx, scene, x, width);
  ctx.restore();
  drawHeaders(ctx, scene);
}

function drawRuler(ctx: CanvasRenderingContext2D, scene: Scene, x: X, px: number, viewEnd: number, width: number) {
  ctx.fillStyle = C.head;
  ctx.fillRect(0, 0, width, RULER_H);
  ctx.fillStyle = C.rulerLine;
  ctx.fillRect(0, RULER_H - 1, width, 1);

  const fps = scene.fps;
  const step = tickStep(px, 96);
  const first = Math.floor(scene.view.start / step) * step;
  const withFrames = step < 1;
  ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textBaseline = "middle";

  // Frame ticks once a frame is wide enough to be told apart.
  const framePx = px / fps;
  if (framePx >= 6) {
    ctx.fillStyle = "#23232a";
    const firstFrame = Math.floor(scene.view.start * fps);
    const lastFrame = Math.ceil(Math.min(viewEnd, scene.sequence.duration) * fps);
    for (let n = firstFrame; n <= lastFrame; n += 1) {
      ctx.fillRect(Math.round(x(n / fps)), RULER_H - 3, 1, 2);
    }
  }

  for (let at = first; at <= Math.min(viewEnd, scene.sequence.duration) + step; at += step) {
    if (at < -EPS) continue;
    const left = Math.round(x(at)) + 0.5;
    ctx.fillStyle = C.rulerLine;
    ctx.fillRect(left - 0.5, RULER_H - 9, 1, 8);
    ctx.fillStyle = C.ruler;
    ctx.fillText(withFrames ? formatTimecode(at, fps) : clock(at), left + 5, RULER_H / 2 - 2);
    // Half-step tick, unlabelled: the eye needs the rhythm, not the number.
    const half = Math.round(x(at + step / 2));
    ctx.fillStyle = C.rulerLine;
    ctx.fillRect(half, RULER_H - 5, 1, 4);
  }
  ctx.textBaseline = "alphabetic";
}

function drawLanes(ctx: CanvasRenderingContext2D, scene: Scene, width: number) {
  const { videoH, audioTop } = scene.geometry;
  ctx.fillStyle = "#101014";
  ctx.fillRect(0, CLIP_TOP - 3, width, videoH + 6);
  ctx.fillStyle = "#0e1012";
  ctx.fillRect(0, audioTop - 1, width, AUDIO_H + 3);
}

function drawClips(ctx: CanvasRenderingContext2D, scene: Scene, x: X, px: number, viewEnd: number) {
  const trim = scene.trim;
  const clips = scene.sequence.clips;
  let dragged: { clip: Clip; piece: Piece; ghost: Piece | null } | null = null;

  for (const clip of clips) {
    if (trim && trim.clip === clip.index) {
      // The edge follows the hand; the other edge stays put.
      const { start, end } = trim.range;
      const piece: Piece = trim.side === "start"
        ? { left: x(clip.outputStart + (start - clip.start)), right: x(clip.outputEnd), source: start, sourceEnd: clip.end }
        : { left: x(clip.outputStart), right: x(clip.outputStart + (end - clip.start)), source: clip.start, sourceEnd: end };
      // What a shortened clip gives up stays on screen as a ghost.
      let ghost: Piece | null = null;
      if (trim.side === "start" && start > clip.start) ghost = { left: x(clip.outputStart), right: piece.left, source: clip.start, sourceEnd: start };
      if (trim.side === "end" && end < clip.end) ghost = { left: piece.right, right: x(clip.outputEnd), source: end, sourceEnd: clip.end };
      dragged = { clip, piece, ghost };
      continue;
    }
    if (clip.outputEnd < scene.view.start || clip.outputStart > viewEnd) continue;
    const piece = { left: x(clip.outputStart), right: x(clip.outputEnd), source: clip.start, sourceEnd: clip.end };
    const selected = !trim && scene.selection?.kind === "clip" && scene.selection.index === clip.index;
    drawPiece(ctx, scene, piece, px, { alpha: trim ? 0.45 : 1, label: clip.label || `Clip ${clip.index + 1}` });
    if (selected) drawSelected(ctx, scene, piece, scene.hover.handle);
    else if (scene.hover.clip === clip.index && !trim) outline(ctx, scene, piece, "rgba(255,255,255,0.22)", 1);
  }

  if (dragged) {
    const { clip, piece, ghost } = dragged;
    if (ghost && ghost.right - ghost.left > 0.5) {
      drawPiece(ctx, scene, ghost, px, { alpha: 0.28, label: "" });
      hatch(ctx, scene, ghost);
    }
    drawPiece(ctx, scene, piece, px, { alpha: 1, label: clip.label || `Clip ${clip.index + 1}` });
    outline(ctx, scene, piece, C.select, 1.5);
  }

  if (!clips.length) {
    ctx.font = "12px Inter, ui-sans-serif, system-ui";
    ctx.fillStyle = C.muted;
    ctx.textAlign = "center";
    ctx.fillText("Nessuna clip nel montaggio", scene.view.width / 2, CLIP_TOP + scene.geometry.videoH / 2);
    ctx.textAlign = "start";
  }
}

/** The picture lane and the sound lane of one piece of footage. */
function drawPiece(
  ctx: CanvasRenderingContext2D, scene: Scene, piece: Piece, px: number,
  style: { alpha: number; label: string },
) {
  const { videoH, audioTop } = scene.geometry;
  // A seam of a pixel either side: in the film's own time the clips touch,
  // and without it thirty of them read as one long strip.
  const left = piece.left + 0.5;
  const w = Math.max(1, piece.right - piece.left - 1);
  if (left + w < HEAD_W || left > scene.view.width) return;
  ctx.save();
  ctx.globalAlpha = style.alpha;

  ctx.save();
  roundRect(ctx, left, CLIP_TOP, w, videoH, 4);
  ctx.clip();
  ctx.fillStyle = C.clip;
  ctx.fillRect(left, CLIP_TOP, w, videoH);
  drawThumbs(ctx, scene, piece.source, left, w, px);
  if (style.label && w > 90) {
    ctx.fillStyle = "rgba(8,8,10,0.62)";
    ctx.font = "500 10px Inter, ui-sans-serif, system-ui";
    const text = trim(ctx, style.label, w - 16);
    const tw = ctx.measureText(text).width;
    roundRect(ctx, left + 4, CLIP_TOP + 4, tw + 10, 16, 3);
    ctx.fill();
    ctx.fillStyle = "rgba(242,241,238,0.9)";
    ctx.textBaseline = "middle";
    ctx.fillText(text, left + 9, CLIP_TOP + 12.5);
    ctx.textBaseline = "alphabetic";
  }
  ctx.restore();
  outlineRect(ctx, left, CLIP_TOP, w, videoH, C.edge);

  ctx.save();
  roundRect(ctx, left, audioTop, w, AUDIO_H, 4);
  ctx.clip();
  ctx.fillStyle = C.audioClip;
  ctx.fillRect(left, audioTop, w, AUDIO_H);
  drawWave(ctx, scene, piece.source, left, w, px);
  drawWords(ctx, scene, piece, left, w, px);
  ctx.restore();
  outlineRect(ctx, left, audioTop, w, AUDIO_H, C.edge);

  ctx.restore();
}

function drawThumbs(ctx: CanvasRenderingContext2D, scene: Scene, source: number, left: number, w: number, px: number) {
  const sheets = scene.sheets;
  const height = scene.geometry.videoH;
  if (!sheets) {
    ctx.fillStyle = C.clipTop;
    ctx.fillRect(left, CLIP_TOP, w, height);
    return;
  }
  const drawWidth = Math.max(8, Math.round((height * sheets.width) / sheets.height));
  const from = Math.max(0, Math.floor((HEAD_W - left) / drawWidth));
  for (let index = from; ; index += 1) {
    const offset = index * drawWidth;
    if (offset > w || left + offset > scene.view.width) break;
    const found = locate(sheets, source + offset / px);
    if (found) {
      ctx.drawImage(found.image, found.sx, found.sy, sheets.width, sheets.height, left + offset, CLIP_TOP, drawWidth, height);
    } else {
      ctx.fillStyle = C.clipTop;
      ctx.fillRect(left + offset, CLIP_TOP, drawWidth, height);
    }
  }
}

function drawWave(ctx: CanvasRenderingContext2D, scene: Scene, source: number, left: number, w: number, px: number) {
  const peaks = scene.peaks;
  if (!peaks) return;
  const top = scene.geometry.audioTop;
  const middle = top + AUDIO_H / 2;
  ctx.fillStyle = C.wave;
  for (let column = Math.max(0, Math.floor(HEAD_W - left)); column < w; column += 1) {
    const screenX = left + column;
    if (screenX > scene.view.width) break;
    const at = source + column / px;
    const loud = Math.pow(peakBetween(peaks, at, at + 1 / px), 0.62);
    const half = Math.max(0.5, loud * (AUDIO_H / 2 - 3));
    ctx.fillRect(screenX, middle - half, 1, half * 2);
  }
}

/** The words under the waveform, once they are wide enough to be read: the
 *  thing a cut is actually placed against. */
function drawWords(ctx: CanvasRenderingContext2D, scene: Scene, piece: Piece, left: number, w: number, px: number) {
  const source = piece.source;
  const end = piece.sourceEnd;
  const top = scene.geometry.audioTop;
  ctx.font = "500 10px Inter, ui-sans-serif, system-ui";
  ctx.textBaseline = "middle";
  const edges = [source, end];
  for (const word of scene.words) {
    if (word.end <= source) continue;
    if (word.t >= end) break;
    const wx = left + (word.t - source) * px;
    const ww = (word.end - word.t) * px;
    const split = edges.some((edge) => edge > word.t + 0.0015 && edge < word.end - 0.0015);
    if (split) {
      ctx.fillStyle = "rgba(255,90,79,0.22)";
      ctx.fillRect(Math.max(left, wx), top, Math.min(ww, left + w - wx), AUDIO_H);
    }
    if (ww < 26) continue;
    ctx.fillStyle = split ? C.split : "rgba(242,241,238,0.78)";
    const text = trim(ctx, word.s, ww - 4);
    ctx.fillText(text, Math.max(wx + 2, left + 2), top + 9);
  }
  ctx.textBaseline = "alphabetic";
}

function hatch(ctx: CanvasRenderingContext2D, scene: Scene, piece: Piece) {
  const top = CLIP_TOP;
  const bottom = scene.geometry.bottom;
  ctx.save();
  ctx.beginPath();
  ctx.rect(piece.left, top, piece.right - piece.left, bottom - top);
  ctx.clip();
  ctx.strokeStyle = "rgba(255,255,255,0.10)";
  ctx.lineWidth = 1;
  for (let at = piece.left - (bottom - top); at < piece.right; at += 7) {
    ctx.beginPath();
    ctx.moveTo(at, bottom);
    ctx.lineTo(at + (bottom - top), top);
    ctx.stroke();
  }
  ctx.restore();
}

function outline(ctx: CanvasRenderingContext2D, scene: Scene, piece: Piece, color: string, lineWidth: number) {
  const inset = lineWidth / 2 + 0.5;
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  roundRect(ctx, piece.left + inset, CLIP_TOP + inset - 1, piece.right - piece.left - inset * 2, scene.geometry.bottom - CLIP_TOP - inset * 2 + 2, 5);
  ctx.stroke();
  ctx.lineWidth = 1;
}

function outlineRect(ctx: CanvasRenderingContext2D, left: number, top: number, w: number, h: number, color: string) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  roundRect(ctx, left + 0.5, top + 0.5, w - 1, h - 1, 4);
  ctx.stroke();
}

/** The chosen clip: a thin light frame, and two slim brackets at its edges
 *  that only come forward under the pointer. Nothing that covers the picture
 *  at the very place a cut is being judged. */
function drawSelected(ctx: CanvasRenderingContext2D, scene: Scene, piece: Piece, hovered: "start" | "end" | null) {
  outline(ctx, scene, piece, C.select, 1.5);
  const w = piece.right - piece.left;
  if (w < HANDLE_W * 4) return;
  const top = CLIP_TOP;
  const height = scene.geometry.bottom - CLIP_TOP;
  for (const side of ["start", "end"] as const) {
    const x0 = side === "start" ? piece.left + 1 : piece.right - 1 - HANDLE_W;
    const hot = hovered === side;
    ctx.fillStyle = hot ? "rgba(242,241,238,0.95)" : "rgba(242,241,238,0.7)";
    roundRect(ctx, x0, top, HANDLE_W, height, side === "start" ? [5, 1, 1, 5] : [1, 5, 5, 1]);
    ctx.fill();
    ctx.fillStyle = "rgba(12,12,15,0.75)";
    ctx.fillRect(x0 + HANDLE_W / 2 - 0.5, top + height / 2 - 7, 1, 14);
  }
}

/** The edge being dragged: one clean line through everything, the magnet's
 *  marks when it has taken hold, and what the move amounts to. */
function drawTrimEdge(ctx: CanvasRenderingContext2D, scene: Scene, x: X, px: number) {
  const trim = scene.trim!;
  const clip = scene.sequence.clips[trim.clip];
  if (!clip) return;
  const { start, end } = trim.range;
  const at = trim.side === "start"
    ? x(clip.outputStart + (start - clip.start))
    : x(clip.outputStart + (end - clip.start));
  const line = Math.round(at) + 0.5;
  const bottom = scene.geometry.bottom;
  const inWord = scene.words.some((word) => {
    const edge = trim.side === "start" ? start : end;
    return edge > word.t + 0.0015 && edge < word.end - 0.0015;
  });
  const color = inWord ? C.split : C.accent;
  ctx.fillStyle = color;
  ctx.fillRect(line - 0.5, 0, 1, bottom + 4);

  if (trim.magnet) {
    for (const [y, dir] of [[CLIP_TOP - 1, 1], [bottom + 1, -1]] as const) {
      ctx.beginPath();
      ctx.moveTo(line - 5, y - dir * 5);
      ctx.lineTo(line + 5, y - dir * 5);
      ctx.lineTo(line, y);
      ctx.closePath();
      ctx.fill();
    }
  }

  const delta = trim.side === "start" ? clip.start - start : end - clip.end;
  const frames = Math.round(delta * scene.fps);
  const sign = frames > 0 ? "+" : frames < 0 ? "−" : "±";
  const label = `${sign}${Math.abs(delta).toFixed(2)}s  ${sign}${Math.abs(frames)}f   ·   ${(end - start).toFixed(2)}s${trim.free ? "   ·   libero" : ""}`;
  ctx.font = "600 10.5px Inter, ui-sans-serif, system-ui";
  const w = ctx.measureText(label).width + 16;
  const bx = Math.min(Math.max(line - w / 2, HEAD_W + 4), scene.view.width - w - 4);
  ctx.fillStyle = "rgba(20,20,24,0.96)";
  roundRect(ctx, bx, RULER_H + 1, w, 18, 5);
  ctx.fill();
  ctx.strokeStyle = inWord ? "rgba(255,90,79,0.6)" : "rgba(216,255,62,0.45)";
  roundRect(ctx, bx + 0.5, RULER_H + 1.5, w - 1, 17, 5);
  ctx.stroke();
  ctx.fillStyle = C.ink;
  ctx.textBaseline = "middle";
  ctx.fillText(label, bx + 8, RULER_H + 10.5);
  ctx.textBaseline = "alphabetic";
  void px;
}

function drawCuts(ctx: CanvasRenderingContext2D, scene: Scene, x: X, viewEnd: number) {
  // A notch per join, coloured by why; the badge with the numbers only for
  // the one under the pointer or chosen. Thirty badges are a wall.
  const y = RULER_H + MARK_H / 2;
  for (const cut of scene.sequence.cuts) {
    if (cut.at < scene.view.start - 1 || cut.at > viewEnd + 1) continue;
    const at = Math.round(x(cut.at)) + 0.5;
    const selected = scene.selection?.kind === "cut" && scene.selection.index === cut.index;
    const hovered = scene.hover.cut === cut.index;
    const style = REASON[cut.reason] ?? REASON.manual;

    ctx.fillStyle = selected ? C.accent : hovered ? "rgba(242,241,238,0.55)" : "rgba(242,241,238,0.14)";
    ctx.fillRect(at - 0.5, CLIP_TOP, 1, scene.geometry.bottom - CLIP_TOP);

    if (!selected && !hovered) {
      ctx.fillStyle = style.dot;
      ctx.beginPath();
      ctx.moveTo(at - 4, y - 3);
      ctx.lineTo(at + 4, y - 3);
      ctx.lineTo(at, y + 3);
      ctx.closePath();
      ctx.fill();
      continue;
    }

    const label = `${style.label}  ${format(cut.removed)}`;
    ctx.font = "600 10px Inter, ui-sans-serif, system-ui";
    const w = ctx.measureText(label).width + 22;
    const bx = Math.min(Math.max(at, HEAD_W + w / 2 + 3), scene.view.width - w / 2 - 3);
    ctx.fillStyle = selected ? C.accent : "#26272e";
    roundRect(ctx, bx - w / 2, y - 8, w, 16, 4);
    ctx.fill();
    ctx.fillStyle = style.dot;
    ctx.beginPath();
    ctx.arc(bx - w / 2 + 9, y, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = selected ? "#11130b" : C.ink;
    ctx.textBaseline = "middle";
    ctx.fillText(label, bx - w / 2 + 16, y + 0.5);
    ctx.textBaseline = "alphabetic";
  }
}

function drawQuestions(ctx: CanvasRenderingContext2D, scene: Scene, x: X, viewEnd: number) {
  const y = scene.geometry.bottom + 8;
  for (const question of scene.questions) {
    const at = outputAt(scene.sequence, question.at);
    if (at < scene.view.start || at > viewEnd) continue;
    ctx.beginPath();
    ctx.arc(x(at), y, 3, 0, Math.PI * 2);
    ctx.fillStyle = question.resolved ? C.questionDone : C.question;
    ctx.fill();
  }
}

/** The ghost of the playhead: where a click would put it, and at what time. */
function drawHover(ctx: CanvasRenderingContext2D, scene: Scene, x: X, px: number) {
  const at = scene.hover.px;
  if (at === null || at < HEAD_W + PAD - 2) return;
  const origin = HEAD_W + PAD;
  const time = Math.max(0, Math.min(scene.sequence.duration, scene.view.start + (at - origin) / px));
  const line = Math.round(x(time)) + 0.5;
  ctx.fillStyle = C.hover;
  ctx.fillRect(line - 0.5, RULER_H, 1, scene.geometry.bottom - RULER_H);
  const label = formatTimecode(time, scene.fps);
  ctx.font = "600 10px ui-monospace, SFMono-Regular, Menlo, monospace";
  const w = ctx.measureText(label).width + 12;
  const bx = Math.min(Math.max(line - w / 2, HEAD_W + 2), scene.view.width - w - 2);
  ctx.fillStyle = "#2a2b33";
  roundRect(ctx, bx, 5, w, 17, 4);
  ctx.fill();
  ctx.fillStyle = C.ink;
  ctx.textBaseline = "middle";
  ctx.fillText(label, bx + 6, 14);
  ctx.textBaseline = "alphabetic";
}

function drawPlayhead(ctx: CanvasRenderingContext2D, scene: Scene, x: X, width: number) {
  const at = x(Math.max(0, scene.playhead));
  if (at < HEAD_W - 8 || at > width + 8) return;
  const left = Math.round(at) + 0.5;
  ctx.fillStyle = C.playhead;
  ctx.fillRect(left - 0.75, 6, 1.5, scene.geometry.bottom - 6);
  ctx.beginPath();
  ctx.moveTo(left - 5.5, 4);
  ctx.lineTo(left + 5.5, 4);
  ctx.quadraticCurveTo(left + 6.5, 4, left + 6.5, 5);
  ctx.lineTo(left + 6.5, 12);
  ctx.lineTo(left, 18);
  ctx.lineTo(left - 6.5, 12);
  ctx.lineTo(left - 6.5, 5);
  ctx.quadraticCurveTo(left - 6.5, 4, left - 5.5, 4);
  ctx.closePath();
  ctx.fill();
}

function drawHeaders(ctx: CanvasRenderingContext2D, scene: Scene) {
  const { videoH, audioTop, height } = scene.geometry;
  ctx.fillStyle = C.head;
  ctx.fillRect(0, 0, HEAD_W, height);
  ctx.fillStyle = C.line;
  ctx.fillRect(HEAD_W - 1, 0, 1, height);
  ctx.font = "600 10px Inter, ui-sans-serif, system-ui";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const [label, y, h] of [["V1", CLIP_TOP, videoH], ["A1", audioTop, AUDIO_H]] as const) {
    ctx.fillStyle = "#1a1a20";
    roundRect(ctx, 8, y + h / 2 - 10, HEAD_W - 16, 20, 4);
    ctx.fill();
    ctx.fillStyle = C.muted;
    ctx.fillText(label, HEAD_W / 2, y + h / 2 + 0.5);
  }
  ctx.textAlign = "start";
  ctx.textBaseline = "alphabetic";
}

// ----------------------------------------------------------------- helpers

const EPS = 1e-6;

function roundRect(
  ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number,
  radius: number | [number, number, number, number],
) {
  const [tl, tr, br, bl] = typeof radius === "number" ? [radius, radius, radius, radius] : radius;
  const limit = Math.max(0, Math.min(w, h) / 2);
  ctx.beginPath();
  ctx.moveTo(x + Math.min(tl, limit), y);
  ctx.arcTo(x + w, y, x + w, y + h, Math.min(tr, limit));
  ctx.arcTo(x + w, y + h, x, y + h, Math.min(br, limit));
  ctx.arcTo(x, y + h, x, y, Math.min(bl, limit));
  ctx.arcTo(x, y, x + w, y, Math.min(tl, limit));
  ctx.closePath();
}

function trim(ctx: CanvasRenderingContext2D, text: string, max: number): string {
  if (ctx.measureText(text).width <= max) return text;
  let cut = text;
  while (cut.length > 1 && ctx.measureText(`${cut}…`).width > max) cut = cut.slice(0, -1);
  return `${cut}…`;
}

function clock(seconds: number): string {
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  const rest = Math.floor(total % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function format(seconds: number): string {
  return seconds >= 10 ? `${seconds.toFixed(0)}s` : `${seconds.toFixed(1)}s`;
}

export type { Clip, Cut };
