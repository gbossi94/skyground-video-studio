import { useEffect, useRef } from "react";
import type { TrimInfo } from "./Timeline";
import { formatTimecode } from "./timeline/time";

interface Props {
  src: string;
  fps: number;
  /** Written by the timeline on every pointer move; read here per frame. */
  trim: { current: TrimInfo | null };
  /** Which edge is moving, or null when nothing is: mounted all the time so
   *  both videos are loaded before the first drag. */
  side: TrimInfo["side"] | null;
}

/** The join being made, both sides of it at once, the way Premiere shows a
 *  trim: the last frame before the cut on the left, the first after it on the
 *  right. Two video elements on the same proxy, each seeking to the middle of
 *  its frame and never queueing more than one seek behind the one in flight,
 *  so a fast hand does not leave the picture a second behind. */
export function TrimView({ src, fps, trim, side }: Props) {
  const left = useRef<HTMLVideoElement>(null);
  const right = useRef<HTMLVideoElement>(null);
  const leftTime = useRef<HTMLSpanElement>(null);
  const rightTime = useRef<HTMLSpanElement>(null);
  const delta = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const seekers = [seeker(left.current), seeker(right.current)];
    const centre = (seconds: number) => (Math.floor(seconds * fps + 1e-6) + 0.5) / fps;
    let frame = 0;
    let shown = "";
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const info = trim.current;
      if (!info) return;
      const key = `${info.left}|${info.right}|${info.delta}|${info.inWord}`;
      if (key === shown) return;
      shown = key;
      [info.left, info.right].forEach((at, index) => {
        const video = index === 0 ? left.current : right.current;
        const label = index === 0 ? leftTime.current : rightTime.current;
        if (video) video.style.visibility = at === null ? "hidden" : "visible";
        if (at !== null) seekers[index](centre(Math.max(0, at)));
        if (label) label.textContent = at === null ? (index === 0 ? "inizio del film" : "fine del film") : formatTimecode(at, fps);
      });
      if (delta.current) {
        const frames = Math.round(info.delta * fps);
        const sign = frames > 0 ? "+" : frames < 0 ? "−" : "±";
        delta.current.textContent = `${sign}${Math.abs(info.delta).toFixed(2)}s · ${sign}${Math.abs(frames)}f`;
        delta.current.classList.toggle("in-word", info.inWord);
      }
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [fps, trim]);

  return (
    <div className="trimview" aria-label="Anteprima del taglio">
      <figure className={side === "end" ? "moving" : ""}>
        <div className="frame"><video ref={left} src={src} muted playsInline preload="auto" /></div>
        <figcaption><b>fine del pezzo</b><span ref={leftTime} /></figcaption>
      </figure>
      <div className="trim-delta" ref={delta} />
      <figure className={side === "start" ? "moving" : ""}>
        <div className="frame"><video ref={right} src={src} muted playsInline preload="auto" /></div>
        <figcaption><b>inizio del pezzo</b><span ref={rightTime} /></figcaption>
      </figure>
    </div>
  );
}

/** A seek that coalesces: while one is in flight only the latest wanted
 *  position is remembered, and it is issued when the first lands. */
function seeker(video: HTMLVideoElement | null) {
  let busy = false;
  let pending: number | null = null;
  const go = (at: number) => {
    if (!video) return;
    if (busy) { pending = at; return; }
    if (Math.abs(video.currentTime - at) < 1e-4) return;
    busy = true;
    video.currentTime = at;
  };
  video?.addEventListener("seeked", () => {
    busy = false;
    if (pending !== null) {
      const next = pending;
      pending = null;
      go(next);
    }
  });
  return go;
}
