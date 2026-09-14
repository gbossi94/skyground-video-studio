import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CutPlan, Question, Transcript, Word } from "../types";

/** How a removed region is coloured. The reason is the information: a pause and
 *  a discarded take are both "cut", but only one of them threw away words. */
const REASON_STYLE: Record<string, { fill: string; label: string }> = {
  silence: { fill: "var(--cut-silence)", label: "pausa" },
  "lead-in": { fill: "var(--cut-silence)", label: "testa" },
  "lead-out": { fill: "var(--cut-silence)", label: "coda" },
  filler: { fill: "var(--cut-filler)", label: "intercalare" },
  retake: { fill: "var(--cut-retake)", label: "ripetizione" },
  manual: { fill: "var(--cut-manual)", label: "scelta tua" },
};

interface Props {
  plan: CutPlan;
  transcript: Transcript;
  playhead: number;
  onSeek: (seconds: number) => void;
  selectedQuestion: Question | null;
}

/** The source laid out on one axis, with everything the engine decided drawn on
 *  top of it. Zoom is a multiplier on the track width, which keeps every
 *  position a pure function of `seconds * scale` — no virtual scrolling maths
 *  and no drift between the layers. */
export function Timeline({ plan, transcript, playhead, onSeek, selectedQuestion }: Props) {
  const [zoom, setZoom] = useState(1);
  const viewport = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(1000);

  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    setWidth(element.clientWidth);
    return () => observer.disconnect();
  }, []);

  const duration = plan.sourceDuration || transcript.duration || 1;
  const trackWidth = Math.max(width, width * zoom);
  const x = useCallback((seconds: number) => (seconds / duration) * trackWidth, [duration, trackWidth]);

  // Keep the selected question in view: answering one is the main loop of this
  // screen, and hunting for it on a zoomed track would be the whole cost of it.
  useEffect(() => {
    if (!selectedQuestion || !viewport.current) return;
    const target = x(selectedQuestion.at) - viewport.current.clientWidth / 2;
    viewport.current.scrollTo({ left: Math.max(0, target), behavior: "smooth" });
  }, [selectedQuestion, x]);

  const seek = (event: React.MouseEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const offset = event.clientX - box.left + (viewport.current?.scrollLeft ?? 0);
    onSeek(Math.max(0, Math.min(duration, (offset / trackWidth) * duration)));
  };

  const ticks = useTicks(duration, trackWidth);
  const visibleWords = useMemo(
    () => (trackWidth / duration > 12 ? transcript.words : []),
    [trackWidth, duration, transcript.words],
  );

  return (
    <div className="timeline">
      <div className="timeline-bar">
        <span className="eyebrow">TIMELINE DEL GIRATO</span>
        <div className="zoom">
          <button onClick={() => setZoom((z) => Math.max(1, z / 1.6))} aria-label="Riduci zoom">
            −
          </button>
          <span>{zoom.toFixed(1)}×</span>
          <button onClick={() => setZoom((z) => Math.min(60, z * 1.6))} aria-label="Aumenta zoom">
            +
          </button>
        </div>
        <Legend plan={plan} />
      </div>

      <div className="timeline-viewport" ref={viewport}>
        <div className="timeline-track" style={{ width: trackWidth }} onClick={seek}>
          <div className="ruler">
            {ticks.map((tick) => (
              <span key={tick} className="tick" style={{ left: x(tick) }}>
                {formatTime(tick)}
              </span>
            ))}
          </div>

          <div className="lane lane-source" title="Il girato originale">
            {plan.removed.map((region, index) => {
              const style = REASON_STYLE[region.reason] ?? REASON_STYLE.manual;
              return (
                <div
                  key={`r${index}`}
                  className="region removed"
                  style={{ left: x(region.start), width: Math.max(1, x(region.end) - x(region.start)), background: style.fill }}
                  title={`${style.label} · ${(region.end - region.start).toFixed(2)}s\n${region.detail}`}
                />
              );
            })}
            {plan.segments.map((segment, index) => (
              <div
                key={`s${index}`}
                className="region kept"
                style={{ left: x(segment.start), width: Math.max(2, x(segment.end) - x(segment.start)) }}
                title={`tenuto · ${(segment.end - segment.start).toFixed(2)}s\n${segment.label}`}
              >
                <span className="region-label">{segment.label}</span>
              </div>
            ))}
          </div>

          <div className="lane lane-words">
            {visibleWords.map((word, index) => (
              <WordChip key={index} word={word} left={x(word.t)} width={x(word.end) - x(word.t)} />
            ))}
          </div>

          <div className="lane lane-questions">
            {plan.questions.map((question) => (
              <div
                key={question.id}
                className={marker(question, selectedQuestion)}
                style={{ left: x(question.at) }}
                title={question.prompt}
              >
                {question.resolved ? "✓" : "?"}
              </div>
            ))}
          </div>

          <div className="playhead" style={{ left: x(playhead) }} />
        </div>
      </div>
    </div>
  );
}

function marker(question: Question, selected: Question | null) {
  const classes = ["question-marker"];
  if (question.resolved) classes.push("resolved");
  if (selected?.id === question.id) classes.push("selected");
  return classes.join(" ");
}

function WordChip({ word, left, width }: { word: Word; left: number; width: number }) {
  // A low probability is worth seeing: it usually means the boundary around
  // that word is less trustworthy than the rest.
  const unsure = word.p < 0.6;
  return (
    <span
      className={unsure ? "word unsure" : "word"}
      style={{ left, width: Math.max(6, width) }}
      title={unsure ? `${word.s} — trascrizione incerta (${(word.p * 100).toFixed(0)}%)` : word.s}
    >
      {word.s}
    </span>
  );
}

function Legend({ plan }: { plan: CutPlan }) {
  const counts = new Map<string, number>();
  for (const region of plan.removed) {
    counts.set(region.reason, (counts.get(region.reason) ?? 0) + (region.end - region.start));
  }
  return (
    <div className="legend">
      <span className="legend-item">
        <i className="swatch kept" /> tenuto {formatTime(plan.stats.outputDuration)}
      </span>
      {[...counts.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([reason, seconds]) => (
          <span className="legend-item" key={reason}>
            <i className="swatch" style={{ background: (REASON_STYLE[reason] ?? REASON_STYLE.manual).fill }} />
            {(REASON_STYLE[reason] ?? REASON_STYLE.manual).label} {formatTime(seconds)}
          </span>
        ))}
    </div>
  );
}

function useTicks(duration: number, trackWidth: number) {
  return useMemo(() => {
    const perPixel = duration / trackWidth;
    const target = perPixel * 110; // roughly one label every 110px
    const step = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300].find((value) => value >= target) ?? 600;
    const ticks: number[] = [];
    for (let at = 0; at <= duration; at += step) ticks.push(at);
    return ticks;
  }, [duration, trackWidth]);
}

export function formatTime(seconds: number) {
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  const rest = total - minutes * 60;
  return minutes > 0 ? `${minutes}:${rest.toFixed(1).padStart(4, "0")}` : `${rest.toFixed(1)}s`;
}
