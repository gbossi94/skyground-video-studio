import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import {
  EPSILON,
  gapAt,
  lastWordUpTo,
  moveBoundary,
  rangeAt,
  splitRange,
  wordsInside,
  type EditState,
  type Rules,
} from "../edit/model";
import { candidates, snap, type Silence } from "../edit/snap";
import type { MediaInfo, Question, Removed, Word } from "../types";
import { loadPeaks, peakBetween, type Peaks } from "./timeline/peaks";
import { loadSheets, locate, type Sheets } from "./timeline/thumbs";
import { formatTime, tickStep } from "./timeline/time";

/** How a removed region is coloured. The reason is the information: a pause
 *  and a discarded take are both "cut", but only one of them threw away words. */
export const REASON_STYLE: Record<string, { fill: string; label: string }> = {
  silence: { fill: "#2f2e2a", label: "pausa" },
  "lead-in": { fill: "#2f2e2a", label: "testa" },
  "lead-out": { fill: "#2f2e2a", label: "coda" },
  filler: { fill: "#4a3f22", label: "intercalare" },
  retake: { fill: "#7a3a2a", label: "ripetizione" },
  manual: { fill: "#3a3560", label: "tolto a mano" },
};

export type Selection = { kind: "range"; index: number } | { kind: "gap"; index: number };

export interface TimelineHandle {
  zoomBy: (factor: number) => void;
  fit: () => void;
  centerOn: (seconds: number) => void;
}

interface Props {
  state: EditState;
  /** The state a drag is showing before it is committed. */
  preview: EditState | null;
  words: Word[];
  silences: Silence[];
  duration: number;
  fps: number;
  rules: Rules;
  removed: Removed[];
  questions: Question[];
  selectedQuestion: Question | null;
  media: MediaInfo | undefined;
  playhead: number;
  playing: boolean;
  selection: Selection | null;
  onSeek: (seconds: number) => void;
  onSelect: (selection: Selection | null) => void;
  onSelectQuestion: (question: Question) => void;
  onPreview: (state: EditState | null) => void;
  onCommit: (label: string, state: EditState) => void;
}

const HEIGHT = 196;
const LANE = {
  ruler: { top: 0, height: 20 },
  thumbs: { top: 24, height: 60 },
  regions: { top: 88, height: 60 },
  words: { top: 152, height: 20 },
  markers: { top: 176, height: 18 },
};
const HANDLE_REACH = 6;
const MAX_PX_PER_SEC = 400;

type Drag =
  | { kind: "scrub" }
  | { kind: "handle"; index: number; side: "start" | "end"; state: EditState | null }
  | null;

/** The footage on one axis, everything the cut decided drawn over it, and
 *  the handles to change it. One canvas: every layer is a function of the
 *  same `x(seconds)`, drawn only for the seconds in view, so a six-minute
 *  take at any zoom costs the same to draw as a six-second one. */
export const EditorTimeline = forwardRef<TimelineHandle, Props>(function EditorTimeline(props, handle) {
  const {
    state, preview, words, silences, duration, fps, rules, removed, questions, selectedQuestion,
    media, playhead, playing, selection, onSeek, onSelect, onSelectQuestion, onPreview, onCommit,
  } = props;
  const shown = preview ?? state;

  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(1000);
  const [pxPerSec, setPxPerSec] = useState(0);
  const [viewStart, setViewStart] = useState(0);
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [sheets, setSheets] = useState<Sheets | null>(null);
  const [, bump] = useState(0);
  const drag = useRef<Drag>(null);
  const hover = useRef<{ x: number; y: number } | null>(null);

  const fitScale = Math.max(1e-3, width / Math.max(duration, 1));
  const scale = pxPerSec || fitScale;
  const snapPoints = useMemo(() => candidates(words, silences), [words, silences]);
  const snapContext = useMemo(() => ({ words, silences, fps, duration }), [words, silences, fps, duration]);

  // ------------------------------------------------------------- resources

  useEffect(() => {
    const element = canvas.current?.parentElement;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(200, entry.contentRect.width)));
    observer.observe(element);
    setWidth(Math.max(200, element.clientWidth));
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!media?.ready || !media.peaks) return;
    let cancelled = false;
    void loadPeaks(media.peaks, media.peaksRate ?? 100)
      .then((loaded) => { if (!cancelled) setPeaks(loaded); })
      .catch(() => { /* the waveform is a help, not a requirement */ });
    return () => { cancelled = true; };
  }, [media?.ready, media?.peaks, media?.peaksRate]);

  useEffect(() => {
    if (!media?.ready || !media.thumbs?.urls.length) return;
    setSheets(loadSheets(media.thumbs, () => bump((n) => n + 1)));
  }, [media?.ready, media?.thumbs]);

  // ------------------------------------------------------------------ view

  const clampView = useCallback((start: number, px: number) => {
    const span = width / px;
    return Math.max(0, Math.min(start, Math.max(0, duration - span)));
  }, [width, duration]);

  const zoomAround = useCallback((factor: number, atX: number) => {
    const before = viewStart + atX / scale;
    const next = Math.max(fitScale, Math.min(MAX_PX_PER_SEC, scale * factor));
    setPxPerSec(next);
    setViewStart(clampView(before - atX / next, next));
  }, [viewStart, scale, fitScale, clampView]);

  useImperativeHandle(handle, () => ({
    zoomBy: (factor) => zoomAround(factor, width / 2),
    fit: () => { setPxPerSec(0); setViewStart(0); },
    centerOn: (seconds) => setViewStart(clampView(seconds - width / scale / 2, scale)),
  }), [zoomAround, width, scale, clampView]);

  // Follow the playhead while it plays, a fifth of the way in from the left.
  useEffect(() => {
    if (!playing) return;
    const x = (playhead - viewStart) * scale;
    if (x < 0 || x > width - 24) setViewStart(clampView(playhead - width / scale / 5, scale));
  }, [playhead, playing, viewStart, scale, width, clampView]);

  // Keep the selected question in view: answering one is the main loop of
  // the review, and hunting for it on a zoomed track would be its whole cost.
  useEffect(() => {
    if (!selectedQuestion) return;
    const x = (selectedQuestion.at - viewStart) * scale;
    if (x < 0 || x > width) setViewStart(clampView(selectedQuestion.at - width / scale / 2, scale));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedQuestion]);

  // The wheel pans; with ⌘ (or ctrl, or a pinch) it zooms around the pointer.
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = element.getBoundingClientRect();
      if (event.ctrlKey || event.metaKey) {
        zoomAround(Math.exp(-event.deltaY * 0.01), event.clientX - box.left);
        return;
      }
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      setViewStart((start) => clampView(start + delta / scale, scale));
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, [zoomAround, clampView, scale]);

  // --------------------------------------------------------------- pointer

  const toSeconds = (clientX: number) => {
    const box = canvas.current!.getBoundingClientRect();
    return viewStart + (clientX - box.left) / scale;
  };

  const hitHandle = (x: number, y: number): { index: number; side: "start" | "end" } | null => {
    if (y < LANE.regions.top || y > LANE.regions.top + LANE.regions.height) return null;
    let best: { index: number; side: "start" | "end"; distance: number } | null = null;
    shown.kept.forEach((range, index) => {
      for (const side of ["start", "end"] as const) {
        const distance = Math.abs((range[side] - viewStart) * scale - x);
        if (distance <= HANDLE_REACH && (!best || distance < best.distance)) best = { index, side, distance };
      }
    });
    return best;
  };

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    const box = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - box.left;
    const y = event.clientY - box.top;
    event.currentTarget.setPointerCapture(event.pointerId);
    const seconds = toSeconds(event.clientX);

    if (y >= LANE.markers.top) {
      const near = questions.find((question) => Math.abs((question.at - viewStart) * scale - x) <= 10);
      if (near) { onSelectQuestion(near); return; }
    }
    const grip = hitHandle(x, y);
    if (grip) {
      drag.current = { kind: "handle", ...grip, state: null };
      return;
    }
    if (y >= LANE.regions.top && y <= LANE.regions.top + LANE.regions.height) {
      const index = rangeAt(shown, seconds);
      onSelect(index !== null ? { kind: "range", index } : { kind: "gap", index: gapAt(shown, seconds) ?? 0 });
    }
    drag.current = { kind: "scrub" };
    onSeek(Math.max(0, Math.min(duration, seconds)));
  };

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - box.left;
    const y = event.clientY - box.top;
    hover.current = { x, y };
    const current = drag.current;
    if (!current) {
      event.currentTarget.style.cursor = hitHandle(x, y) ? "col-resize" : "text";
      return;
    }
    const seconds = toSeconds(event.clientX);
    if (current.kind === "scrub") {
      onSeek(Math.max(0, Math.min(duration, seconds)));
      return;
    }
    const snapped = snap(seconds, snapContext, snapPoints, scale);
    const moved = moveBoundary(state, words, current.index, current.side, snapped, rules);
    if (moved) {
      current.state = moved;
      onPreview(moved);
      onSeek(current.side === "start" ? moved.kept[current.index].start : moved.kept[current.index].end);
    }
  };

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const current = drag.current;
    drag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (current?.kind === "handle") {
      onPreview(null);
      if (current.state) onCommit(current.side === "start" ? "sposta l'inizio" : "sposta la fine", current.state);
    }
  };

  const onDoubleClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const y = event.clientY - box.top;
    if (y < LANE.regions.top || y > LANE.regions.top + LANE.regions.height) return;
    const seconds = toSeconds(event.clientX);
    const index = rangeAt(state, seconds);
    if (index === null) return;
    const afterWord = lastWordUpTo(words, seconds);
    const split = splitRange(state, words, index, afterWord, rules);
    if (split) onCommit("dividi", split);
  };

  // ------------------------------------------------------------------ draw

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    let frame = 0;
    const paint = () => {
      frame = 0;
      draw(element, {
        width, scale, viewStart, duration, shown, words, removed, questions, selectedQuestion,
        playhead, selection, peaks, sheets, rules,
      });
    };
    frame = requestAnimationFrame(paint);
    return () => { if (frame) cancelAnimationFrame(frame); };
  });

  return (
    <div className="editor-timeline">
      <canvas
        ref={canvas}
        height={HEIGHT}
        style={{ width: "100%", height: HEIGHT, display: "block" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={onDoubleClick}
        onPointerLeave={() => { hover.current = null; }}
        aria-label="Timeline del girato"
      />
    </div>
  );
});

// ------------------------------------------------------------------ painting

interface Scene {
  width: number;
  scale: number;
  viewStart: number;
  duration: number;
  shown: EditState;
  words: Word[];
  removed: Removed[];
  questions: Question[];
  selectedQuestion: Question | null;
  playhead: number;
  selection: Selection | null;
  peaks: Peaks | null;
  sheets: Sheets | null;
  rules: Rules;
}

function draw(element: HTMLCanvasElement, scene: Scene) {
  const dpr = window.devicePixelRatio || 1;
  const { width, scale, viewStart, duration } = scene;
  if (element.width !== Math.round(width * dpr) || element.height !== Math.round(HEIGHT * dpr)) {
    element.width = Math.round(width * dpr);
    element.height = Math.round(HEIGHT * dpr);
  }
  const ctx = element.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, HEIGHT);
  const x = (seconds: number) => (seconds - viewStart) * scale;
  const viewEnd = viewStart + width / scale;

  drawRuler(ctx, scene, x, viewEnd);
  drawThumbs(ctx, scene, x, viewEnd);
  drawRegions(ctx, scene, x, viewEnd);
  drawWords(ctx, scene, x, viewEnd);
  drawMarkers(ctx, scene, x, viewEnd);

  // The playhead, over everything.
  const px = x(scene.playhead);
  if (px >= -1 && px <= width + 1) {
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(Math.round(px) - 0.5, LANE.ruler.height, 1.5, HEIGHT - LANE.ruler.height);
    ctx.beginPath();
    ctx.moveTo(px - 5, LANE.ruler.height - 6);
    ctx.lineTo(px + 5, LANE.ruler.height - 6);
    ctx.lineTo(px, LANE.ruler.height + 1);
    ctx.closePath();
    ctx.fill();
  }
  void duration;
}

function drawRuler(ctx: CanvasRenderingContext2D, scene: Scene, x: (s: number) => number, viewEnd: number) {
  const { ruler } = LANE;
  ctx.fillStyle = "#111110";
  ctx.fillRect(0, ruler.top, scene.width, ruler.height);
  ctx.strokeStyle = "#2a2926";
  ctx.beginPath();
  ctx.moveTo(0, ruler.height - 0.5);
  ctx.lineTo(scene.width, ruler.height - 0.5);
  ctx.stroke();
  const step = tickStep(scene.scale);
  const first = Math.floor(scene.viewStart / step) * step;
  ctx.font = "9px Inter, ui-sans-serif, system-ui";
  ctx.fillStyle = "#6f6d66";
  ctx.strokeStyle = "#35342f";
  for (let at = first; at <= Math.min(viewEnd, scene.duration) + step; at += step) {
    const px = x(at);
    ctx.beginPath();
    ctx.moveTo(Math.round(px) + 0.5, 8);
    ctx.lineTo(Math.round(px) + 0.5, ruler.height);
    ctx.stroke();
    ctx.fillText(formatTime(at), px + 4, 12);
  }
}

function drawThumbs(ctx: CanvasRenderingContext2D, scene: Scene, x: (s: number) => number, viewEnd: number) {
  const { thumbs } = LANE;
  ctx.fillStyle = "#0b0b0a";
  ctx.fillRect(0, thumbs.top, scene.width, thumbs.height);
  const { sheets } = scene;
  if (!sheets) return;
  const drawWidth = Math.round((thumbs.height * sheets.width) / sheets.height);
  const step = Math.max(1 / sheets.fps, Math.ceil(drawWidth / scene.scale / (1 / sheets.fps)) / sheets.fps);
  const first = Math.floor(scene.viewStart / step) * step;
  for (let at = Math.max(0, first); at < Math.min(viewEnd, scene.duration); at += step) {
    const found = locate(sheets, at);
    if (!found) continue;
    ctx.drawImage(found.image, found.sx, found.sy, sheets.width, sheets.height,
      Math.round(x(at)), thumbs.top, drawWidth, thumbs.height);
  }
}

function drawRegions(ctx: CanvasRenderingContext2D, scene: Scene, x: (s: number) => number, viewEnd: number) {
  const { regions } = LANE;
  const { shown, words, removed, peaks, selection } = scene;
  ctx.fillStyle = "#111110";
  ctx.fillRect(0, regions.top, scene.width, regions.height);

  // The gaps, each with the reason it has when the plan knows it.
  let cursor = 0;
  const gaps: { start: number; end: number; index: number }[] = [];
  shown.kept.forEach((range, index) => {
    if (range.start > cursor + EPSILON) gaps.push({ start: cursor, end: range.start, index });
    cursor = range.end;
  });
  if (cursor < scene.duration - EPSILON) gaps.push({ start: cursor, end: scene.duration, index: shown.kept.length });
  for (const gap of gaps) {
    if (gap.end < scene.viewStart || gap.start > viewEnd) continue;
    const known = removed.find((item) => Math.abs(item.start - gap.start) < 0.02 && Math.abs(item.end - gap.end) < 0.02);
    const reason = known?.reason ?? (wordsInside(words, gap.start, gap.end).length ? "manual" : "silence");
    const style = REASON_STYLE[reason] ?? REASON_STYLE.manual;
    const left = x(gap.start);
    const right = x(gap.end);
    ctx.fillStyle = style.fill;
    ctx.fillRect(left, regions.top, Math.max(1, right - left), regions.height);
    if (selection?.kind === "gap" && selection.index === gap.index) {
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(left + 0.75, regions.top + 0.75, Math.max(1, right - left) - 1.5, regions.height - 1.5);
      ctx.lineWidth = 1;
    }
    if (right - left > 40) {
      ctx.fillStyle = "#c9c5ba";
      ctx.font = "9px Inter, ui-sans-serif, system-ui";
      ctx.fillText(`${style.label} · ${(gap.end - gap.start).toFixed(2)}s`, left + 5, regions.top + 12);
    }
  }

  // The waveform, over kept and removed alike; brighter where it is kept.
  if (peaks) {
    const middle = regions.top + regions.height / 2;
    const columns = Math.ceil(scene.width);
    for (let column = 0; column < columns; column += 1) {
      const from = scene.viewStart + column / scene.scale;
      const to = from + 1 / scene.scale;
      if (from > scene.duration) break;
      const loud = Math.pow(peakBetween(peaks, from, to), 0.6);
      const half = Math.max(0.5, loud * (regions.height / 2 - 4));
      const kept = rangeAt(shown, from) !== null;
      ctx.fillStyle = kept ? "rgba(216,255,62,0.55)" : "rgba(150,147,139,0.35)";
      ctx.fillRect(column, middle - half, 1, half * 2);
    }
  }

  // The kept ranges: a tint, an edge, and a handle at either end.
  shown.kept.forEach((range, index) => {
    if (range.end < scene.viewStart || range.start > viewEnd) return;
    const left = x(range.start);
    const right = x(range.end);
    ctx.fillStyle = "rgba(216,255,62,0.10)";
    ctx.fillRect(left, regions.top, right - left, regions.height);
    const selected = selection?.kind === "range" && selection.index === index;
    ctx.strokeStyle = selected ? "#ffffff" : "#d8ff3e";
    ctx.lineWidth = selected ? 1.5 : 1;
    ctx.strokeRect(left + 0.5, regions.top + 0.5, right - left - 1, regions.height - 1);
    ctx.lineWidth = 1;
    for (const px of [left, right]) {
      ctx.fillStyle = "#d8ff3e";
      ctx.fillRect(px - 2, regions.top, 4, regions.height);
      ctx.fillStyle = "#11130b";
      ctx.fillRect(px - 0.5, regions.top + regions.height / 2 - 6, 1, 12);
    }
  });
}

function drawWords(ctx: CanvasRenderingContext2D, scene: Scene, x: (s: number) => number, viewEnd: number) {
  const { words: lane } = LANE;
  const { words, shown } = scene;
  if (scene.scale < 40) {
    // Too small to read: a bar per word, so the rhythm of speech still shows.
    for (const word of words) {
      if (word.end < scene.viewStart || word.t > viewEnd) continue;
      ctx.fillStyle = rangeAt(shown, word.t) !== null ? "#7f9a2a" : "#3a3935";
      ctx.fillRect(x(word.t), lane.top + 8, Math.max(1, (word.end - word.t) * scene.scale), 4);
    }
    return;
  }
  ctx.font = "10px Inter, ui-sans-serif, system-ui";
  ctx.textBaseline = "middle";
  for (const word of words) {
    if (word.end < scene.viewStart || word.t > viewEnd) continue;
    const left = x(word.t);
    const wide = Math.max(6, (word.end - word.t) * scene.scale);
    const kept = rangeAt(shown, word.t) !== null;
    const unsure = word.p < 0.6;
    ctx.fillStyle = kept ? "#1c1b19" : "#141413";
    ctx.fillRect(left, lane.top + 1, wide, lane.height - 2);
    ctx.strokeStyle = unsure ? "#7a5a2a" : "#2c2b27";
    ctx.strokeRect(left + 0.5, lane.top + 1.5, wide - 1, lane.height - 3);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left + 2, lane.top, wide - 4, lane.height);
    ctx.clip();
    ctx.fillStyle = unsure ? "#d8b779" : kept ? "#b9b6ad" : "#6a6862";
    ctx.fillText(word.s, left + 3, lane.top + lane.height / 2);
    ctx.restore();
  }
  ctx.textBaseline = "alphabetic";
}

function drawMarkers(ctx: CanvasRenderingContext2D, scene: Scene, x: (s: number) => number, viewEnd: number) {
  const { markers } = LANE;
  ctx.font = "bold 10px Inter, ui-sans-serif, system-ui";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const question of scene.questions) {
    if (question.at < scene.viewStart || question.at > viewEnd) continue;
    const px = x(question.at);
    const cy = markers.top + markers.height / 2;
    ctx.beginPath();
    ctx.arc(px, cy, 8, 0, Math.PI * 2);
    ctx.fillStyle = question.resolved ? "#3f5c34" : "#ff6b3d";
    ctx.fill();
    if (scene.selectedQuestion?.id === question.id) {
      ctx.strokeStyle = "#d8ff3e";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.lineWidth = 1;
    }
    ctx.fillStyle = question.resolved ? "#cfe9c2" : "#1b0d07";
    ctx.fillText(question.resolved ? "✓" : "?", px, cy + 0.5);
  }
  ctx.textAlign = "start";
  ctx.textBaseline = "alphabetic";
}
