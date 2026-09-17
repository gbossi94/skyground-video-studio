import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";

export interface PlayerHandle {
  play: () => void;
  pause: () => void;
  toggle: () => void;
  /** To an instant in the source, frame-exact. */
  seek: (seconds: number) => void;
  /** By whole frames, from wherever it is. */
  step: (frames: number) => void;
  /** J and L: backwards is emulated a frame at a time, forwards uses the
   *  element's rate; either, pressed again, goes faster. */
  shuttle: (direction: -1 | 1) => void;
  /** One stretch of the source, removed regions and all: to hear a take. */
  playRange: (start: number, end: number) => void;
  stop: () => void;
}

interface Props {
  src: string | null;
  fps: number;
  /** Read on every presented frame, so an edit that changed a moment ago is
   *  what the player skips between — no stale closure, no re-render. */
  getSegments: () => { start: number; end: number }[];
  onTime: (seconds: number) => void;
  onPlayingChange: (playing: boolean) => void;
  onError?: (message: string) => void;
}

type Mode = "idle" | "edit" | "range" | "back";

/** Plays the edit out of the untouched proxy.
 *
 *  Nothing is rendered: when the presented frame reaches the end of a kept
 *  range the element is sent to the start of the next. The check runs on
 *  `requestVideoFrameCallback` — the frame that was actually shown — not on
 *  `timeupdate`, whose quarter-second granularity let a quarter second of
 *  removed material through at every cut. Seeks are exact to the frame in
 *  Chrome (`fastSeek` is the inexact one and is never used), issued one at a
 *  time: while one is in flight the latest request waits its turn. */
export const Player = forwardRef<PlayerHandle, Props>(function Player(
  { src, fps, getSegments, onTime, onPlayingChange, onError },
  handle,
) {
  const video = useRef<HTMLVideoElement>(null);
  const mode = useRef<Mode>("idle");
  const range = useRef<{ start: number; end: number } | null>(null);
  const rate = useRef(1);
  const wanted = useRef<number | null>(null);
  const seeking = useRef(false);
  const callbacks = useRef({ getSegments, onTime, onPlayingChange, onError });
  callbacks.current = { getSegments, onTime, onPlayingChange, onError };

  const issue = (seconds: number) => {
    const element = video.current;
    if (!element) return;
    seeking.current = true;
    element.currentTime = Math.max(0, seconds);
  };

  const seek = (seconds: number) => {
    if (seeking.current) {
      wanted.current = seconds;
      return;
    }
    issue(seconds);
  };

  /** The middle of the frame `seconds` falls in. Seeking to the middle rather
   *  than the edge is what makes the seek land on the frame meant: rounding
   *  can never tip it into the neighbour. */
  const frameCentre = (seconds: number) => (Math.floor(seconds * fps + 1e-6) + 0.5) / fps;

  /** `frames` whole frames from where the player is (or is on its way to).
   *  Counted in frame numbers, not seconds: adding 1/fps to a time already at
   *  a frame's middle and rounding again stepped two frames at a time. */
  const frameStep = (from: number, frames: number) =>
    (Math.floor(from * fps + 1e-6) + frames + 0.5) / fps;

  const setPlaying = (value: boolean) => callbacks.current.onPlayingChange(value);

  const pause = () => {
    const element = video.current;
    mode.current = "idle";
    range.current = null;
    rate.current = 1;
    if (element) {
      element.playbackRate = 1;
      if (!element.paused) element.pause();
    }
    setPlaying(false);
  };

  const play = () => {
    const element = video.current;
    if (!element) return;
    mode.current = "edit";
    range.current = null;
    element.playbackRate = rate.current;
    const next = nextStart(callbacks.current.getSegments(), element.currentTime);
    if (next !== null) {
      const segments = callbacks.current.getSegments();
      const last = segments[segments.length - 1];
      // At or past the end of the cut: start over from the top.
      if (last && next >= last.end - 0.001) issue(segments[0]?.start ?? 0);
      else issue(next);
    }
    void element.play().then(() => setPlaying(true)).catch(() => setPlaying(false));
  };

  useImperativeHandle(handle, () => ({
    play,
    pause,
    toggle: () => {
      const element = video.current;
      if (!element) return;
      if (element.paused && mode.current !== "back") play();
      else pause();
    },
    seek: (seconds) => {
      if (mode.current !== "idle") pause();
      seek(seconds);
    },
    step: (frames) => {
      const element = video.current;
      if (!element) return;
      if (mode.current !== "idle") pause();
      seek(frameStep(wanted.current ?? element.currentTime, frames));
    },
    shuttle: (direction) => {
      const element = video.current;
      if (!element) return;
      if (direction === 1) {
        if (mode.current === "edit" && !element.paused) rate.current = Math.min(4, rate.current * 2);
        else rate.current = 1;
        play();
        return;
      }
      if (mode.current === "back") rate.current = Math.min(4, rate.current * 2);
      else {
        if (!element.paused) element.pause();
        rate.current = 1;
        mode.current = "back";
        setPlaying(true);
      }
      seek(frameStep(wanted.current ?? element.currentTime, -rate.current));
    },
    playRange: (start, end) => {
      const element = video.current;
      if (!element) return;
      mode.current = "range";
      range.current = { start, end };
      issue(start);
      void element.play().then(() => setPlaying(true)).catch(() => setPlaying(false));
    },
    stop: pause,
  }));

  // The frame loop: what was presented, and what to do next.
  useEffect(() => {
    const element = video.current;
    if (!element) return;
    let handleId = 0;
    let stopped = false;
    const onFrame = (_now: number, metadata: VideoFrameCallbackMetadata) => {
      if (stopped) return;
      const at = metadata.mediaTime;
      callbacks.current.onTime(at);
      const current = mode.current;
      if (current === "edit" && !element.paused) {
        const segments = callbacks.current.getSegments();
        const next = nextStart(segments, at);
        if (next !== null) {
          const last = segments[segments.length - 1];
          if (last && next >= last.end - 0.001) pause();
          else if (Math.abs(next - at) > 0.02) seek(next);
        }
      } else if (current === "range" && range.current && at >= range.current.end) {
        const back = range.current.start;
        pause();
        seek(back);
      } else if (current === "back") {
        if (at <= 1 / fps) pause();
        else seek(frameStep(at, -rate.current));
      }
      handleId = element.requestVideoFrameCallback?.(onFrame) ?? 0;
    };
    if (typeof element.requestVideoFrameCallback === "function") {
      handleId = element.requestVideoFrameCallback(onFrame);
    } else {
      // Not Chrome: the coarse clock, so the edit still plays.
      const tick = () => onFrame(0, { mediaTime: element.currentTime } as VideoFrameCallbackMetadata);
      element.addEventListener("timeupdate", tick);
      return () => element.removeEventListener("timeupdate", tick);
    }
    return () => {
      stopped = true;
      if (handleId) element.cancelVideoFrameCallback?.(handleId);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, fps]);

  const onSeeked = () => {
    const element = video.current;
    if (!element) return;
    seeking.current = false;
    callbacks.current.onTime(element.currentTime);
    if (wanted.current !== null) {
      const next = wanted.current;
      wanted.current = null;
      if (Math.abs(next - element.currentTime) > 1e-4) issue(next);
    }
  };

  const showFirstFrame = () => {
    const element = video.current;
    if (!element || element.currentTime > 0.01) return;
    const first = callbacks.current.getSegments()[0];
    issue(frameCentre(first ? first.start : 0));
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
        onSeeked={onSeeked}
        onEnded={pause}
        onPause={() => { if (mode.current === "edit") setPlaying(false); }}
        onLoadedMetadata={showFirstFrame}
        onError={() => callbacks.current.onError?.("il video non si carica: il link potrebbe essere scaduto")}
        playsInline
        preload="auto"
        muted={false}
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
