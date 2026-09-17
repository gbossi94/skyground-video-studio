import { useEffect, useRef } from "react";
import type { Clock } from "./Player";
import { formatTimecode } from "./timeline/time";

interface Props {
  clock: Clock;
  /** The film's length, read every frame: a trim changes it as you drag. */
  duration: Clock;
  fps: number;
  playing: boolean;
  disabled: boolean;
  onAction: (action: "prev-cut" | "frame-back" | "toggle" | "frame-forward" | "next-cut") => void;
}

/** Under the picture: where you are in the film, and the five controls that
 *  move you. The timecode writes itself into the DOM from the playhead's own
 *  animation frame — putting it through React re-rendered the editor thirty
 *  times a second, and that was most of the stutter. */
export function Transport({ clock, duration, fps, playing, disabled, onAction }: Props) {
  const readout = useRef<HTMLSpanElement>(null);
  const total = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    let frame = 0;
    let at = -1;
    let length = -1;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const now = Math.round(clock.current * fps);
      if (now !== at && readout.current) {
        at = now;
        readout.current.textContent = formatTimecode(clock.current, fps);
      }
      const end = Math.round(duration.current * fps);
      if (end !== length && total.current) {
        length = end;
        total.current.textContent = `/ ${formatTimecode(duration.current, fps)}`;
      }
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [clock, duration, fps]);

  return (
    <div className="transport">
      <div className="clock">
        <span className="now" ref={readout}>{formatTimecode(clock.current, fps)}</span>
        <span className="total" ref={total}>/ {formatTimecode(duration.current, fps)}</span>
      </div>
      <div className="keys">
        <button className="key" disabled={disabled} title="Taglio precedente ([)" onClick={() => onAction("prev-cut")} aria-label="Taglio precedente">
          <Icon name="prev" />
        </button>
        <button className="key" disabled={disabled} title="Un fotogramma indietro (←)" onClick={() => onAction("frame-back")} aria-label="Un fotogramma indietro">
          <Icon name="back" />
        </button>
        <button className="key play" disabled={disabled} title={playing ? "Pausa (spazio)" : "Riproduci il montaggio (spazio)"} onClick={() => onAction("toggle")} aria-label={playing ? "Pausa" : "Riproduci"}>
          <Icon name={playing ? "pause" : "play"} />
        </button>
        <button className="key" disabled={disabled} title="Un fotogramma avanti (→)" onClick={() => onAction("frame-forward")} aria-label="Un fotogramma avanti">
          <Icon name="forward" />
        </button>
        <button className="key" disabled={disabled} title="Taglio successivo (])" onClick={() => onAction("next-cut")} aria-label="Taglio successivo">
          <Icon name="next" />
        </button>
      </div>
      <span className="hint">montaggio, senza le parti tolte</span>
    </div>
  );
}

function Icon({ name }: { name: "prev" | "back" | "play" | "pause" | "forward" | "next" }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "currentColor", "aria-hidden": true } as const;
  switch (name) {
    case "play":
      return <svg {...common}><path d="M5 3.2v9.6a.6.6 0 0 0 .92.5l7.3-4.8a.6.6 0 0 0 0-1l-7.3-4.8A.6.6 0 0 0 5 3.2Z" /></svg>;
    case "pause":
      return <svg {...common}><rect x="4" y="3" width="3" height="10" rx="1" /><rect x="9" y="3" width="3" height="10" rx="1" /></svg>;
    case "back":
      return <svg {...common}><path d="M10.6 3.3 5.4 7.5a.6.6 0 0 0 0 1l5.2 4.2a.6.6 0 0 0 1-.5V3.8a.6.6 0 0 0-1-.5Z" /></svg>;
    case "forward":
      return <svg {...common}><path d="M5.4 3.3 10.6 7.5a.6.6 0 0 1 0 1l-5.2 4.2a.6.6 0 0 1-1-.5V3.8a.6.6 0 0 1 1-.5Z" /></svg>;
    case "prev":
      return <svg {...common}><rect x="3" y="3.5" width="1.8" height="9" rx=".9" /><path d="M12.2 3.5 6.6 7.6a.5.5 0 0 0 0 .8l5.6 4.1a.5.5 0 0 0 .8-.4V3.9a.5.5 0 0 0-.8-.4Z" /></svg>;
    case "next":
      return <svg {...common}><rect x="11.2" y="3.5" width="1.8" height="9" rx=".9" /><path d="M3.8 3.5 9.4 7.6a.5.5 0 0 1 0 .8l-5.6 4.1a.5.5 0 0 1-.8-.4V3.9a.5.5 0 0 1 .8-.4Z" /></svg>;
  }
}
