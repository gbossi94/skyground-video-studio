import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import { moveBoundary, splitRange, type EditState, type Rules } from "../edit/model";
import { candidates, snap, type Silence } from "../edit/snap";
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
import { tickStep } from "./timeline/time";

export type Selection = { kind: "clip"; index: number } | { kind: "cut"; index: number };

export interface TimelineHandle {
  zoomBy: (factor: number) => void;
  fit: () => void;
  reveal: (output: number) => void;
  redraw: () => void;
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
  onScrub: (output: number) => void;
  onSelect: (selection: Selection | null) => void;
  onCommit: (label: string, state: EditState) => void;
  onPreview: (preview: { state: EditState; sequence: Sequence } | null) => void;
  onZoom: (pxPerSec: number, fit: number) => void;
}

// ------------------------------------------------------------------ the look

const C = {
  bg: "#0e0e11",
  track: "#131317",
  trackLine: "#26262c",
  ruler: "#7d7b76",
  rulerLine: "#2c2c33",
  clip: "#20222a",
  clipTop: "#2a2d37",
  clipEdge: "#34373f",
  clipHover: "#3d414c",
  accent: "#d8ff3e",
  accentDim: "rgba(216,255,62,0.16)",
  wave: "rgba(216,255,62,0.55)",
  waveSelected: "rgba(216,255,62,0.85)",
  ink: "#f2f1ee",
  muted: "#96938b",
  playhead: "#ffffff",
  question: "#ff6b3d",
  questionDone: "#4a6b3c",
};

const REASON: Record<string, { dot: string; label: string }> = {
  silence: { dot: "#6b6b72", label: "pausa" },
  "lead-in": { dot: "#6b6b72", label: "testa" },
  "lead-out": { dot: "#6b6b72", label: "coda" },
  filler: { dot: "#c9a227", label: "intercalare" },
  retake: { dot: "#e2703a", label: "ripetizione" },
  manual: { dot: "#8b7cff", label: "tolto a mano" },
};

const RULER_H = 26;
const MARKER_H = 18;
const CLIP_TOP = RULER_H + MARKER_H + 10;
const CLIP_H = 118;
const WAVE_H = 28;
const HEIGHT = CLIP_TOP + CLIP_H + 22;
const HANDLE_W = 11;
const GRAB = 9;
const MIN_PX_PER_SEC = 4;
/** A gutter at either end of the axis. Without it the first clip, the ruler's
 *  first label and the playhead at zero are all cut in half by the edge. */
const PAD = 16;
const MAX_PX_PER_SEC = 600;

type Drag =
  | { kind: "scrub" }
  | { kind: "trim"; clip: Clip; side: "start" | "end"; next: EditState | null; moved: boolean }
  | null;

/** The film on one axis: the clips butted against each other, the joins where
 *  something was taken out, and the handles that move them.
 *
 *  It draws itself on a single canvas from a mutable scene, inside one
 *  animation frame, so the playhead can run at the video's frame rate without
 *  React re-rendering anything. Everything is a function of `x(output)`, and
 *  only the seconds on screen are ever drawn.
 */
export const Timeline = forwardRef<TimelineHandle, Props>(function Timeline(props, handle) {
  const {
    state, words, silences, rules, source, media, fps, questions, selection,
    playhead, playing, snapping, onScrub, onSelect, onCommit, onPreview, onZoom,
  } = props;

  const canvas = useRef<HTMLCanvasElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag>(null);
  const hover = useRef<{ clip: number | null; cut: number | null; handle: "start" | "end" | null }>({
    clip: null, cut: null, handle: null,
  });
  const view = useRef({ pxPerSec: 0, start: 0, width: 1000, height: HEIGHT });
  const peaks = useRef<Peaks | null>(null);
  const sheets = useRef<Sheets | null>(null);
  const dirty = useRef(true);
  const scene = useRef({ sequence: null as Sequence | null, preview: null as Sequence | null });

  const sequence = useMemo(() => build(state, words, source), [state, words, source]);
  scene.current.sequence = sequence;
  const snapPoints = useMemo(() => candidates(words, silences), [words, silences]);
  const snapContext = useMemo(
    () => ({ words, silences, fps, duration: source.sourceDuration }),
    [words, silences, fps, source.sourceDuration],
  );

  const invalidate = useCallback(() => { dirty.current = true; }, []);
  const shown = () => scene.current.preview ?? scene.current.sequence ?? sequence;

  const span = () => Math.max(120, view.current.width - PAD * 2);
  const fitScale = () => Math.max(MIN_PX_PER_SEC, span() / Math.max(shown().duration, 0.5));
  const scale = () => view.current.pxPerSec || fitScale();
  const x = (output: number) => PAD + (output - view.current.start) * scale();
  const timeAt = (px: number) => view.current.start + (px - PAD) / scale();

  const clampStart = (start: number, px: number) =>
    Math.max(0, Math.min(start, Math.max(0, shown().duration - span() / px)));

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

  useEffect(invalidate, [sequence, selection, questions, invalidate]);

  // ------------------------------------------------------------------ zoom

  const zoomAround = useCallback((factor: number, atPx: number) => {
    const before = timeAt(atPx);
    const fit = fitScale();
    const next = Math.max(fit, Math.min(MAX_PX_PER_SEC, scale() * factor));
    view.current.pxPerSec = next;
    view.current.start = clampStart(before - atPx / next, next);
    onZoom(next, fit);
    invalidate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invalidate, onZoom]);

  useImperativeHandle(handle, () => ({
    zoomBy: (factor) => zoomAround(factor, view.current.width / 2),
    fit: () => {
      view.current.pxPerSec = 0;
      view.current.start = 0;
      onZoom(fitScale(), fitScale());
      invalidate();
    },
    reveal: (output) => {
      const px = x(output);
      if (px >= 40 && px <= view.current.width - 40) return;
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
        zoomAround(Math.exp(-event.deltaY * 0.0125), event.clientX - box.left);
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
    if (py >= RULER_H && py < CLIP_TOP) {
      const cut = current.cuts.find((item) => Math.abs(x(item.at) - px) <= 11);
      if (cut) found.cut = cut.index;
      return found;
    }
    if (py < CLIP_TOP || py > CLIP_TOP + CLIP_H) return found;
    // The selected clip owns its edges. Without this the clip before it wins
    // the point — the grab zones touch — and its left handle cannot be taken.
    const chosen = selection?.kind === "clip" ? current.clips[selection.index] : undefined;
    const onChosen = chosen
      && px >= x(chosen.outputStart) - GRAB && px <= x(chosen.outputEnd) + GRAB;
    const clip = onChosen
      ? chosen
      : current.clips.find((item) => px >= x(item.outputStart) - GRAB && px <= x(item.outputEnd) + GRAB);
    if (!clip) {
      const cut = current.cuts.find((item) => Math.abs(x(item.at) - px) <= 8);
      if (cut) found.cut = cut.index;
      return found;
    }
    found.clip = clip.index;
    if (Math.abs(px - x(clip.outputStart)) <= GRAB) found.handle = "start";
    else if (Math.abs(px - x(clip.outputEnd)) <= GRAB) found.handle = "end";
    return found;
  };

  const local = (event: React.PointerEvent | React.MouseEvent) => {
    const box = canvas.current!.getBoundingClientRect();
    return { px: event.clientX - box.left, py: event.clientY - box.top };
  };

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    const { px, py } = local(event);
    canvas.current?.setPointerCapture(event.pointerId);
    const found = hit(px, py);

    if (found.clip !== null && found.handle && selection?.kind === "clip" && selection.index === found.clip) {
      const clip = shown().clips[found.clip];
      drag.current = { kind: "trim", clip, side: found.handle, next: null, moved: false };
      return;
    }
    if (found.cut !== null) {
      onSelect({ kind: "cut", index: found.cut });
      const cut = shown().cuts.find((item) => item.index === found.cut);
      if (cut) onScrub(cut.at);
      return;
    }
    if (found.clip !== null) {
      onSelect({ kind: "clip", index: found.clip });
      if (found.handle) {
        const clip = shown().clips[found.clip];
        drag.current = { kind: "trim", clip, side: found.handle, next: null, moved: false };
        return;
      }
    } else if (py > CLIP_TOP + CLIP_H) {
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
      const selectedClip = selection?.kind === "clip" ? selection.index : null;
      const canTrim = found.handle !== null && found.clip === selectedClip;
      const changed =
        found.clip !== hover.current.clip || found.cut !== hover.current.cut ||
        (canTrim ? found.handle : null) !== hover.current.handle;
      hover.current = { clip: found.clip, cut: found.cut, handle: canTrim ? found.handle : null };
      if (canvas.current) {
        canvas.current.style.cursor = canTrim ? "col-resize" : found.cut !== null ? "pointer" : found.clip !== null ? "default" : "text";
      }
      if (changed) invalidate();
      return;
    }

    if (current.kind === "scrub") {
      onScrub(Math.max(0, Math.min(shown().duration, timeAt(px))));
      return;
    }

    // Trimming: the pointer's film time, read against the clip as it was when
    // the drag began, is how much of its head or tail to give back or take.
    const wantedOutput = timeAt(px);
    const anchor = current.side === "start" ? current.clip.outputStart : current.clip.outputEnd;
    const wantedSource = (current.side === "start" ? current.clip.start : current.clip.end) + (wantedOutput - anchor);
    const target = snapping ? snap(wantedSource, snapContext, snapPoints, scale()) : wantedSource;
    const next = moveBoundary(state, words, current.clip.index, current.side, target, rules);
    if (next) {
      current.next = next;
      current.moved = true;
      scene.current.preview = build(next, words, source);
      onPreview({ state: next, sequence: scene.current.preview });
      const moved = scene.current.preview.clips[current.clip.index];
      if (moved) onScrub(current.side === "start" ? moved.outputStart : moved.outputEnd);
      invalidate();
    }
  };

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const current = drag.current;
    drag.current = null;
    canvas.current?.releasePointerCapture(event.pointerId);
    if (current?.kind === "trim") {
      scene.current.preview = null;
      onPreview(null);
      if (current.moved && current.next) {
        onCommit(current.side === "start" ? "taglia l'inizio" : "taglia la fine", current.next);
      }
      invalidate();
    }
  };

  const onDoubleClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const { px, py } = local(event);
    if (py < CLIP_TOP || py > CLIP_TOP + CLIP_H) return;
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
      const active = playing || drag.current !== null;
      if (!dirty.current && !active) return;
      dirty.current = false;
      const element = canvas.current;
      if (!element) return;
      paint(element, {
        view: view.current,
        sequence: shown(),
        words,
        questions,
        selection,
        hover: hover.current,
        playhead: playhead.current,
        peaks: peaks.current,
        sheets: sheets.current,
        trimming: drag.current?.kind === "trim" ? drag.current.side : null,
        trimmingClip: drag.current?.kind === "trim" ? drag.current.clip.index : null,
      });
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [words, questions, selection, playing, sequence]);

  return (
    <div className="tl" ref={wrap}>
      <canvas
        ref={canvas}
        height={HEIGHT}
        style={{ width: "100%", height: HEIGHT }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={() => {
          if (drag.current) return;
          hover.current = { clip: null, cut: null, handle: null };
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
  sequence: Sequence;
  words: Word[];
  questions: Question[];
  selection: Selection | null;
  hover: { clip: number | null; cut: number | null; handle: "start" | "end" | null };
  playhead: number;
  peaks: Peaks | null;
  sheets: Sheets | null;
  trimming: "start" | "end" | null;
  trimmingClip: number | null;
}

function paint(element: HTMLCanvasElement, scene: Scene) {
  const dpr = window.devicePixelRatio || 1;
  const { width } = scene.view;
  if (element.width !== Math.round(width * dpr) || element.height !== Math.round(HEIGHT * dpr)) {
    element.width = Math.round(width * dpr);
    element.height = Math.round(HEIGHT * dpr);
  }
  const ctx = element.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, width, HEIGHT);

  const px = scene.view.pxPerSec
    || Math.max(MIN_PX_PER_SEC, Math.max(120, width - PAD * 2) / Math.max(scene.sequence.duration, 0.5));
  const x = (output: number) => PAD + (output - scene.view.start) * px;
  const viewEnd = scene.view.start + (width - PAD) / px;

  drawRuler(ctx, scene, x, px, viewEnd, width);
  drawTrack(ctx, scene, x, px, viewEnd, width);
  drawCuts(ctx, scene, x, viewEnd);
  drawQuestions(ctx, scene, x, viewEnd);
  drawPlayhead(ctx, scene, x, width);
}

function drawRuler(
  ctx: CanvasRenderingContext2D, scene: Scene, x: (t: number) => number,
  px: number, viewEnd: number, width: number,
) {
  ctx.fillStyle = C.track;
  ctx.fillRect(0, 0, width, RULER_H);
  ctx.fillStyle = C.rulerLine;
  ctx.fillRect(0, RULER_H - 1, width, 1);

  const step = tickStep(px, 96);
  const first = Math.floor(scene.view.start / step) * step;
  ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textBaseline = "middle";
  for (let at = first; at <= Math.min(viewEnd, scene.sequence.duration) + step; at += step) {
    if (at < -EPS) continue;
    if (x(at) < PAD - 1) continue;
    const left = Math.round(x(at)) + 0.5;
    ctx.fillStyle = C.rulerLine;
    ctx.fillRect(left, RULER_H - 7, 1, 6);
    ctx.fillStyle = C.ruler;
    ctx.fillText(clock(at), left + 5, RULER_H / 2 - 1);
    // Half-step tick, unlabelled: the eye needs the rhythm, not the number.
    const half = Math.round(x(at + step / 2)) + 0.5;
    ctx.fillStyle = C.rulerLine;
    ctx.fillRect(half, RULER_H - 4, 1, 3);
  }
  ctx.textBaseline = "alphabetic";
}

function drawTrack(
  ctx: CanvasRenderingContext2D, scene: Scene, x: (t: number) => number,
  px: number, viewEnd: number, width: number,
) {
  ctx.fillStyle = C.track;
  ctx.fillRect(0, CLIP_TOP - 6, width, CLIP_H + 12);

  for (const clip of scene.sequence.clips) {
    if (clip.outputEnd < scene.view.start || clip.outputStart > viewEnd) continue;
    // A seam of a pixel either side. In the film's own time the clips touch,
    // and without the seam thirty of them read as one long strip — which is
    // the one thing this timeline exists to show.
    const left = x(clip.outputStart) + 1;
    const right = x(clip.outputEnd) - 1;
    const w = Math.max(2, right - left);
    const selected = scene.selection?.kind === "clip" && scene.selection.index === clip.index;
    const hovered = scene.hover.clip === clip.index;

    ctx.save();
    roundRect(ctx, left, CLIP_TOP, w, CLIP_H, 7);
    ctx.clip();

    ctx.fillStyle = C.clip;
    ctx.fillRect(left, CLIP_TOP, w, CLIP_H);
    drawThumbs(ctx, scene, clip, left, w, px);
    drawWave(ctx, scene, clip, left, w, px, selected);

    // A label only where there is room for it to be read.
    if (w > 96) {
      const shade = ctx.createLinearGradient(0, CLIP_TOP, 0, CLIP_TOP + 22);
      shade.addColorStop(0, "rgba(10,10,12,0.85)");
      shade.addColorStop(1, "rgba(10,10,12,0)");
      ctx.fillStyle = shade;
      ctx.fillRect(left, CLIP_TOP, w, 22);
      ctx.font = "600 10px Inter, ui-sans-serif, system-ui";
      ctx.fillStyle = "rgba(242,241,238,0.92)";
      ctx.textBaseline = "middle";
      const label = clip.label || `Clip ${clip.index + 1}`;
      ctx.fillText(trim(ctx, label, w - 14), left + 7, CLIP_TOP + 11);
      ctx.textBaseline = "alphabetic";
    }
    ctx.restore();

    if (selected) {
      ctx.save();
      ctx.shadowColor = "rgba(216,255,62,0.35)";
      ctx.shadowBlur = 14;
      ctx.shadowOffsetY = 2;
      roundRect(ctx, left + 1, CLIP_TOP + 1, w - 2, CLIP_H - 2, 7);
      ctx.strokeStyle = C.accent;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.restore();
    } else {
      roundRect(ctx, left + 0.5, CLIP_TOP + 0.5, w - 1, CLIP_H - 1, 7);
      ctx.strokeStyle = hovered ? C.clipHover : C.clipEdge;
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.lineWidth = 1;

    // Handles need a clip wide enough to hold them: on a three-second clip
    // seen whole they would be the clip. Below that the border carries the
    // selection, and the edges are still grabbable.
    if (selected && w >= 4 * HANDLE_W) {
      drawHandles(ctx, left, right, scene.trimmingClip === clip.index ? scene.trimming : null, scene.hover.handle);
    } else if (selected) {
      ctx.fillStyle = C.accent;
      ctx.fillRect(left, CLIP_TOP, 2.5, CLIP_H);
      ctx.fillRect(right - 2.5, CLIP_TOP, 2.5, CLIP_H);
    }
  }

  if (!scene.sequence.clips.length) {
    ctx.font = "12px Inter, ui-sans-serif, system-ui";
    ctx.fillStyle = C.muted;
    ctx.textAlign = "center";
    ctx.fillText("Nessuna clip nel montaggio", width / 2, CLIP_TOP + CLIP_H / 2);
    ctx.textAlign = "start";
  }
}

function drawThumbs(ctx: CanvasRenderingContext2D, scene: Scene, clip: Clip, left: number, w: number, px: number) {
  const sheets = scene.sheets;
  const top = CLIP_TOP;
  const height = CLIP_H - WAVE_H;
  if (!sheets) {
    ctx.fillStyle = C.clipTop;
    ctx.fillRect(left, top, w, height);
    return;
  }
  const drawWidth = Math.round((height * sheets.width) / sheets.height);
  const from = Math.max(0, Math.floor((scene.view.start - clip.outputStart) / (drawWidth / px)));
  for (let index = from; ; index += 1) {
    const offset = index * drawWidth;
    if (offset > w) break;
    const found = locate(sheets, clip.start + offset / px);
    if (found) {
      ctx.drawImage(found.image, found.sx, found.sy, sheets.width, sheets.height,
        left + offset, top, drawWidth, height);
    } else {
      ctx.fillStyle = C.clipTop;
      ctx.fillRect(left + offset, top, drawWidth, height);
    }
    if (left + offset > scene.view.width) break;
  }
}

function drawWave(
  ctx: CanvasRenderingContext2D, scene: Scene, clip: Clip,
  left: number, w: number, px: number, selected: boolean,
) {
  const top = CLIP_TOP + CLIP_H - WAVE_H;
  ctx.fillStyle = "rgba(8,9,11,0.72)";
  ctx.fillRect(left, top, w, WAVE_H);
  const peaks = scene.peaks;
  if (!peaks) return;
  const middle = top + WAVE_H / 2;
  ctx.fillStyle = selected ? C.waveSelected : C.wave;
  const from = Math.max(0, Math.floor(scene.view.start - clip.outputStart) * px);
  for (let column = Math.max(0, from); column < w; column += 1) {
    const screenX = left + column;
    if (screenX < -1) continue;
    if (screenX > scene.view.width) break;
    const at = clip.start + column / px;
    const loud = Math.pow(peakBetween(peaks, at, at + 1 / px), 0.62);
    const half = Math.max(0.5, loud * (WAVE_H / 2 - 2));
    ctx.fillRect(screenX, middle - half, 1, half * 2);
  }
}

function drawHandles(
  ctx: CanvasRenderingContext2D, left: number, right: number,
  trimming: "start" | "end" | null, hovered: "start" | "end" | null,
) {
  for (const side of ["start", "end"] as const) {
    const at = side === "start" ? left : right;
    const x0 = side === "start" ? at : at - HANDLE_W;
    const active = trimming === side || (!trimming && hovered === side);
    ctx.fillStyle = C.accent;
    roundRect(ctx, x0, CLIP_TOP, HANDLE_W, CLIP_H, side === "start" ? [7, 0, 0, 7] : [0, 7, 7, 0]);
    ctx.fill();
    ctx.fillStyle = active ? "rgba(17,19,11,0.95)" : "rgba(17,19,11,0.55)";
    for (const offset of [-5, 0, 5]) {
      ctx.fillRect(x0 + HANDLE_W / 2 - 1, CLIP_TOP + CLIP_H / 2 + offset - 4, 2, 8);
    }
  }
}

function drawCuts(ctx: CanvasRenderingContext2D, scene: Scene, x: (t: number) => number, viewEnd: number) {
  // A badge per join is a wall of numbers the moment a film has thirty of
  // them. Show the badge where it has room to be read, a dot where it does
  // not, and always the badge for the one under the pointer.
  const visible = scene.sequence.cuts.filter((cut) => cut.at >= scene.view.start - 1 && cut.at <= viewEnd + 1);
  const room = new Map<number, boolean>();
  let lastBadge = -Infinity;
  for (const cut of visible) {
    const at = x(cut.at);
    const fits = at - lastBadge > 62;
    room.set(cut.index, fits);
    if (fits) lastBadge = at;
  }

  for (const cut of visible) {
    const at = Math.round(x(cut.at)) + 0.5;
    const selected = scene.selection?.kind === "cut" && scene.selection.index === cut.index;
    const hovered = scene.hover.cut === cut.index;
    const style = REASON[cut.reason] ?? REASON.manual;
    const y = RULER_H + MARKER_H / 2 + 1;

    // The join itself: a hairline through the clips, so the eye finds it.
    ctx.fillStyle = selected ? C.accent : hovered ? "rgba(242,241,238,0.5)" : "rgba(242,241,238,0.16)";
    ctx.fillRect(at - 0.5, CLIP_TOP, 1, CLIP_H);

    if (!room.get(cut.index) && !selected && !hovered) {
      ctx.fillStyle = style.dot;
      ctx.beginPath();
      ctx.arc(at, y, 2.5, 0, Math.PI * 2);
      ctx.fill();
      continue;
    }

    // Its badge, above the track: the dot says why, the number says how much.
    const wide = selected || hovered;
    const label = wide ? `${style.label} ${format(cut.removed)}` : format(cut.removed);
    ctx.font = "600 9.5px Inter, ui-sans-serif, system-ui";
    const w = ctx.measureText(label).width + 20;
    // Kept inside the canvas: a badge for the join at zero used to be sliced
    // in half by the left edge.
    const bx = Math.min(Math.max(at, w / 2 + 3), scene.view.width - w / 2 - 3);
    ctx.fillStyle = selected ? C.accent : wide ? "#2b2e36" : "#1d1f26";
    roundRect(ctx, bx - w / 2, y - 8, w, 16, 8);
    ctx.fill();
    if (!selected) {
      ctx.strokeStyle = "rgba(242,241,238,0.10)";
      roundRect(ctx, bx - w / 2 + 0.5, y - 7.5, w - 1, 15, 8);
      ctx.stroke();
    }
    ctx.fillStyle = style.dot;
    ctx.beginPath();
    ctx.arc(bx - w / 2 + 9, y, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = selected ? "#11130b" : C.ink;
    ctx.textBaseline = "middle";
    ctx.fillText(label, bx - w / 2 + 16, y);
    ctx.textBaseline = "alphabetic";
  }
}

function drawQuestions(ctx: CanvasRenderingContext2D, scene: Scene, x: (t: number) => number, viewEnd: number) {
  const y = CLIP_TOP + CLIP_H + 8;
  for (const question of scene.questions) {
    const at = outputAt(scene.sequence, question.at);
    if (at < scene.view.start || at > viewEnd) continue;
    ctx.beginPath();
    ctx.arc(x(at), y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = question.resolved ? C.questionDone : C.question;
    ctx.fill();
  }
}

function drawPlayhead(ctx: CanvasRenderingContext2D, scene: Scene, x: (t: number) => number, width: number) {
  const at = x(Math.max(0, scene.playhead));
  if (at < -8 || at > width + 8) return;
  const left = Math.round(at) + 0.5;
  ctx.fillStyle = C.playhead;
  ctx.fillRect(left - 0.5, RULER_H - 8, 1.5, HEIGHT - RULER_H + 8);
  ctx.beginPath();
  ctx.moveTo(left - 6, RULER_H - 15);
  ctx.lineTo(left + 6, RULER_H - 15);
  ctx.lineTo(left + 6, RULER_H - 10);
  ctx.lineTo(left, RULER_H - 4);
  ctx.lineTo(left - 6, RULER_H - 10);
  ctx.closePath();
  ctx.fill();
}

// ----------------------------------------------------------------- helpers

const EPS = 1e-6;

function roundRect(
  ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number,
  radius: number | [number, number, number, number],
) {
  const [tl, tr, br, bl] = typeof radius === "number" ? [radius, radius, radius, radius] : radius;
  const limit = Math.min(w, h) / 2;
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
