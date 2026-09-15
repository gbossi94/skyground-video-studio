import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import type { CutPlan } from "../types";

interface Props {
  src: string | null;
  plan: CutPlan;
  playhead: number;
  playing: boolean;
  onTime: (seconds: number) => void;
  onPlayingChange: (playing: boolean) => void;
}

/** What the rest of the screen can ask the player to do. `playRange` is the
 *  reason this handle exists: choosing between two takes means listening to
 *  them, one after the other, without leaving the question. */
export interface PreviewHandle {
  playRange: (start: number, end: number) => void;
  toggleEdit: () => void;
  stop: () => void;
}

/** Plays the *proposed* edit out of the untouched source: when the playhead
 *  reaches the end of a kept segment it jumps to the start of the next one. So
 *  what you hear is the cut, without anything having been rendered yet. */
export const Preview = forwardRef<PreviewHandle, Props>(function Preview(
  { src, plan, playhead, playing, onTime, onPlayingChange },
  handle,
) {
  const video = useRef<HTMLVideoElement>(null);
  const segments = useMemo(() => plan.segments, [plan.segments]);
  //: While a single option is playing, skipping removed regions is wrong —
  //: the point is to hear that take exactly as it was spoken.
  const range = useRef<{ start: number; end: number } | null>(null);

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    if (Math.abs(element.currentTime - playhead) > 0.35) element.currentTime = playhead;
  }, [playhead]);

  // Deliberately not driven by an effect: a browser only grants playback to a
  // call made inside the click that asked for it, and an effect runs after.
  const start = (from: number | null) => {
    const element = video.current;
    if (!element) return;
    if (from !== null) element.currentTime = from;
    void element
      .play()
      .then(() => onPlayingChange(true))
      .catch(() => onPlayingChange(false));
  };

  const stop = () => {
    const element = video.current;
    if (element && !element.paused) element.pause();
    range.current = null;
    onPlayingChange(false);
  };

  useImperativeHandle(handle, () => ({
    playRange(from: number, to: number) {
      range.current = { start: from, end: to };
      start(from);
    },
    toggleEdit() {
      const element = video.current;
      if (!element) return;
      if (element.paused) {
        range.current = null;
        start(null);
      } else {
        stop();
      }
    },
    stop,
  }));

  useEffect(() => {
    const element = video.current;
    if (element && !playing && !element.paused) element.pause();
  }, [playing]);

  // Un video fermo su `preload="metadata"` non disegna niente: si apre lo
  // strumento e si vede un rettangolo nero, che è indistinguibile da un guasto.
  // Portarlo sul primo fotogramma che il montaggio tiene lo rende un'immagine.
  const showFirstFrame = () => {
    const element = video.current;
    if (!element || element.currentTime > 0.01) return;
    element.currentTime = segments.length ? segments[0].start : 0;
  };

  const tick = () => {
    const element = video.current;
    if (!element) return;
    const at = element.currentTime;
    const window = range.current;
    if (window) {
      if (at >= window.end) {
        stop();
        onTime(window.start);
        return;
      }
      onTime(at);
      return;
    }
    if (playing) {
      const next = nextStart(segments, at);
      if (next !== null && Math.abs(next - at) > 0.02) {
        element.currentTime = next;
        onTime(next);
        return;
      }
    }
    onTime(at);
  };

  if (!src) {
    return (
      <div className="phone phone-empty">
        <p>Il girato non è ancora riproducibile</p>
        <small>
          Il file della camera è in un formato che i browser non decodificano. Il worker ne sta
          preparando una copia riproducibile.
        </small>
      </div>
    );
  }

  return (
    <div className="phone">
      <video
        ref={video}
        src={src}
        onTimeUpdate={tick}
        onEnded={stop}
        onLoadedMetadata={showFirstFrame}
        playsInline
        preload="metadata"
      />
    </div>
  );
});

/** Where playback should be, given a position in the source: the same instant
 *  if it is inside a kept segment, otherwise the start of the next one. */
export function nextStart(segments: { start: number; end: number }[], at: number): number | null {
  for (const segment of segments) {
    if (at < segment.start - 0.001) return segment.start;
    if (at <= segment.end) return null; // already inside a kept piece
  }
  return segments.length ? segments[segments.length - 1].end : null;
}
