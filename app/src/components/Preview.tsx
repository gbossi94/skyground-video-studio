import { useEffect, useMemo, useRef } from "react";
import type { CutPlan } from "../types";

interface Props {
  src: string | null;
  plan: CutPlan;
  playhead: number;
  playing: boolean;
  onTime: (seconds: number) => void;
  onPlayingChange: (playing: boolean) => void;
}

/** Plays the *proposed* edit out of the untouched source: when the playhead
 *  reaches the end of a kept segment it jumps to the start of the next one. So
 *  what you hear is the cut, without anything having been rendered yet. */
export function Preview({ src, plan, playhead, playing, onTime, onPlayingChange }: Props) {
  const video = useRef<HTMLVideoElement>(null);
  const segments = useMemo(() => plan.segments, [plan.segments]);

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    if (Math.abs(element.currentTime - playhead) > 0.35) element.currentTime = playhead;
  }, [playhead]);

  // Deliberately not driven by an effect: a browser only grants playback to a
  // call made inside the click that asked for it, and an effect runs after.
  const toggle = () => {
    const element = video.current;
    if (!element) return;
    if (element.paused) {
      void element
        .play()
        .then(() => onPlayingChange(true))
        .catch(() => onPlayingChange(false));
    } else {
      element.pause();
      onPlayingChange(false);
    }
  };

  useEffect(() => {
    const element = video.current;
    if (element && !playing && !element.paused) element.pause();
  }, [playing]);

  const tick = () => {
    const element = video.current;
    if (!element) return;
    const at = element.currentTime;
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

  return (
    <div className="preview">
      <div className="phone">
        {src ? (
          <video ref={video} src={src} onTimeUpdate={tick} playsInline preload="metadata" />
        ) : (
          <div className="phone-empty">Il girato non è disponibile in anteprima</div>
        )}
      </div>
      <div className="transport">
        <button onClick={toggle} disabled={!src}>
          {playing ? "Pausa" : "Riproduci il montaggio"}
        </button>
        <span className="hint">Salta le parti rimosse</span>
      </div>
    </div>
  );
}

/** Where playback should be, given a position in the source: the same instant
 *  if it is inside a kept segment, otherwise the start of the next one. */
export function nextStart(segments: { start: number; end: number }[], at: number): number | null {
  for (const segment of segments) {
    if (at < segment.start - 0.001) return segment.start;
    if (at <= segment.end) return null; // already inside a kept piece
  }
  return segments.length ? segments[segments.length - 1].end : null;
}
