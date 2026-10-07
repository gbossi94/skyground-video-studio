/** Seconds as people read them. */

export function formatTime(seconds: number) {
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  const rest = total - minutes * 60;
  return minutes > 0 ? `${minutes}:${rest.toFixed(1).padStart(4, "0")}` : `${rest.toFixed(1)}s`;
}

/** mm:ss:ff — the timecode an editor reads off the transport. */
export function formatTimecode(seconds: number, fps: number) {
  const total = Math.max(0, seconds);
  const frames = Math.round(total * fps);
  const ff = frames % fps;
  const wholeSeconds = Math.floor(frames / fps);
  const mm = Math.floor(wholeSeconds / 60);
  const ss = wholeSeconds % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}:${String(ff).padStart(2, "0")}`;
}

/** Ruler steps that keep roughly one label every `target` pixels. */
export function tickStep(pxPerSec: number, target = 110): number {
  const wanted = target / pxPerSec;
  return [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300].find((value) => value >= wanted) ?? 600;
}
