import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { outputAt, sourceAt, type Sequence } from "../edit/sequence";

/** The playhead, in the film's time. A mutable box rather than React state:
 *  it changes thirty times a second and the canvas reads it inside its own
 *  animation frame, so nothing re-renders while the film plays. */
export type Clock = { current: number };

export interface PlayerHandle {
  play: () => void;
  pause: () => void;
  toggle: () => void;
  /** To an instant of the film. */
  seek: (output: number) => void;
  step: (frames: number) => void;
  shuttle: (direction: -1 | 1) => void;
  /** One stretch of the *source*, cuts and all: to hear a take as it was. */
  playRange: (start: number, end: number) => void;
  stop: () => void;
  rate: () => number;
}

interface Props {
  src: string | null;
  fps: number;
  clock: Clock;
  /** Read every frame, so an edit made a moment ago is the film being played. */
  getSequence: () => Sequence;
  onPlayingChange: (playing: boolean) => void;
  onError?: (message: string) => void;
  onEnded?: () => void;
}

type Mode = "idle" | "edit" | "range" | "back";

export const Player = forwardRef<PlayerHandle, Props>(function Player(
  { src, fps, clock, getSequence, onPlayingChange, onError, onEnded },
  handle,
) {
  const video = useRef<HTMLVideoElement>(null);
  const mode = useRef<Mode>("idle");
  const range = useRef<{ start: number; end: number } | null>(null);
  const rate = useRef(1);
  /** Which clip is on screen. Kept rather than looked up from the presented
   *  frame: a seek to the head of a clip lands on the frame *containing* its
   *  start, whose timestamp is up to a frame earlier: looked up, that frame
   *  belongs to no clip, and the player seeks to the same place for ever —
   *  which is what made it stop at the first join instead of playing on. */
  const playingIndex = useRef(0);
  const pending = useRef<number | null>(null);
  const seeking = useRef(false);
  const bag = useRef({ getSequence, onPlayingChange, onError, onEnded });
  bag.current = { getSequence, onPlayingChange, onError, onEnded };

  /** The middle of the frame an instant falls in: seeking there can never be
   *  tipped into the neighbouring frame by rounding. */
  const centre = (source: number) => (Math.floor(source * fps + 1e-6) + 0.5) / fps;

  const issue = (source: number) => {
    const element = video.current;
    if (!element) return;
    seeking.current = true;
    element.currentTime = Math.max(0, source);
  };

  const seekSource = (source: number) => {
    if (seeking.current) { pending.current = source; return; }
    issue(source);
  };

  const seekOutput = (output: number) => {
    const sequence = bag.current.getSequence();
    const clamped = Math.max(0, Math.min(output, sequence.duration));
    clock.current = clamped;
    seekSource(centre(sourceAt(sequence, clamped)));
  };

  const pause = () => {
    const element = video.current;
    mode.current = "idle";
    range.current = null;
    rate.current = 1;
    if (element) {
      element.playbackRate = 1;
      if (!element.paused) element.pause();
    }
    bag.current.onPlayingChange(false);
  };

  const play = () => {
    const element = video.current;
    if (!element) return;
    const sequence = bag.current.getSequence();
    mode.current = "edit";
    range.current = null;
    element.playbackRate = rate.current;
    // At the end of the film, playing means playing it again from the top.
    if (clock.current >= sequence.duration - 0.05) clock.current = 0;
    playingIndex.current = Math.max(0, sequence.clips.findIndex((clip) => clock.current < clip.outputEnd - 1e-6));
    issue(centre(sourceAt(sequence, clock.current)));
    void element.play().then(() => bag.current.onPlayingChange(true)).catch(() => bag.current.onPlayingChange(false));
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
    seek: (output) => {
      if (mode.current !== "idle") pause();
      seekOutput(output);
    },
    step: (frames) => {
      if (mode.current !== "idle") pause();
      seekOutput((Math.round(clock.current * fps) + frames) / fps);
    },
    shuttle: (direction) => {
      const element = video.current;
      if (!element) return;
      if (direction === 1) {
        rate.current = mode.current === "edit" && !element.paused ? Math.min(8, rate.current * 2) : 1;
        play();
        return;
      }
      if (mode.current === "back") rate.current = Math.min(8, rate.current * 2);
      else {
        if (!element.paused) element.pause();
        rate.current = 1;
        mode.current = "back";
        bag.current.onPlayingChange(true);
      }
      seekOutput((Math.round(clock.current * fps) - rate.current) / fps);
    },
    playRange: (start, end) => {
      const element = video.current;
      if (!element) return;
      mode.current = "range";
      range.current = { start, end };
      issue(start);
      void element.play().then(() => bag.current.onPlayingChange(true)).catch(() => bag.current.onPlayingChange(false));
    },
    stop: pause,
    rate: () => rate.current,
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [fps]);

  // What was actually presented, and what to do next. On the frame callback
  // rather than `timeupdate`, whose quarter-second beat let a quarter second
  // of removed material through at every join.
  useEffect(() => {
    const element = video.current;
    if (!element) return;
    let id = 0;
    let stopped = false;

    const onFrame = (_now: number, metadata: VideoFrameCallbackMetadata) => {
      if (stopped) return;
      const source = metadata.mediaTime;
      const sequence = bag.current.getSequence();
      const current = mode.current;

      if (current === "range" && range.current) {
        clock.current = outputAt(sequence, source);
        if (source >= range.current.end) {
          const back = range.current.start;
          pause();
          issue(back);
        }
      } else if (current === "edit" && !element.paused) {
        const clips = sequence.clips;
        if (!clips.length) { pause(); return; }
        const slack = 1.5 / fps;
        let index = Math.min(playingIndex.current, clips.length - 1);
        let clip = clips[index];
        // The cut can change under a running film — a trim, an undo. When the
        // frame on screen is nowhere near the clip we thought we were in,
        // find it again rather than jumping somewhere absurd.
        if (source < clip.start - slack || source > clip.end + slack) {
          const found = clips.findIndex((item) => source >= item.start - slack && source < item.end);
          if (found >= 0) { index = found; clip = clips[found]; }
        }
        playingIndex.current = index;

        if (source >= clip.end - 1e-3) {
          const following = clips[index + 1];
          if (!following) {
            clock.current = sequence.duration;
            pause();
            bag.current.onEnded?.();
          } else {
            // Straight on to the next piece, without stopping: this is the
            // whole trick of playing an edit out of untouched footage.
            playingIndex.current = index + 1;
            clock.current = following.outputStart;
            seekSource(centre(following.start));
          }
        } else {
          clock.current = clip.outputStart + Math.max(0, source - clip.start);
        }
      } else if (current === "back") {
        clock.current = outputAt(sequence, source);
        if (clock.current <= 1 / fps) pause();
        else seekOutput((Math.round(clock.current * fps) - rate.current) / fps);
      } else {
        clock.current = outputAt(sequence, source);
      }
      id = element.requestVideoFrameCallback?.(onFrame) ?? 0;
    };

    if (typeof element.requestVideoFrameCallback === "function") {
      id = element.requestVideoFrameCallback(onFrame);
      return () => { stopped = true; if (id) element.cancelVideoFrameCallback?.(id); };
    }
    const tick = () => onFrame(0, { mediaTime: element.currentTime } as VideoFrameCallbackMetadata);
    element.addEventListener("timeupdate", tick);
    return () => { stopped = true; element.removeEventListener("timeupdate", tick); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, fps]);

  const onSeeked = () => {
    const element = video.current;
    if (!element) return;
    seeking.current = false;
    if (pending.current !== null) {
      const next = pending.current;
      pending.current = null;
      if (Math.abs(next - element.currentTime) > 1e-4) issue(next);
      return;
    }
    if (mode.current === "idle") clock.current = outputAt(bag.current.getSequence(), element.currentTime);
  };

  // A paused video with no frame decoded is a black rectangle, which reads as
  // a fault. Put it on the film's first frame as soon as it can be.
  const showFirstFrame = () => {
    const element = video.current;
    if (!element || element.currentTime > 0.01) return;
    issue(centre(sourceAt(bag.current.getSequence(), clock.current)));
  };

  if (!src) {
    return (
      <div className="screen screen-empty">
        <p>Il girato non è ancora riproducibile</p>
        <small>Il file della camera è in un formato che i browser non decodificano. Il worker ne sta preparando una copia.</small>
      </div>
    );
  }

  return (
    <div className="screen">
      <video
        ref={video}
        src={src}
        onSeeked={onSeeked}
        onEnded={pause}
        onPause={() => { if (mode.current === "edit") bag.current.onPlayingChange(false); }}
        onLoadedMetadata={showFirstFrame}
        onError={() => bag.current.onError?.("il video non si carica: il link potrebbe essere scaduto")}
        playsInline
        preload="auto"
      />
    </div>
  );
});
