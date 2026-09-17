import type { Clip, Cut } from "../edit/sequence";
import type { Word } from "../types";
import { formatTimecode } from "./timeline/time";

const REASON_LABEL: Record<string, string> = {
  silence: "Pausa",
  "lead-in": "Silenzio iniziale",
  "lead-out": "Silenzio finale",
  filler: "Intercalare",
  retake: "Ripetizione",
  manual: "Tolto a mano",
};

interface Props {
  clip: Clip | null;
  cut: Cut | null;
  words: Word[];
  fps: number;
  onSplit: () => void;
  onRemove: () => void;
  onRestore: () => void;
  onListen: () => void;
}

/** What is selected, in numbers and in words, with the two or three things
 *  you would want to do to it. Nothing is selected most of the time, so the
 *  empty state is the one that has to teach the tool. */
export function Inspector({ clip, cut, words, fps, onSplit, onRemove, onRestore, onListen }: Props) {
  if (cut) {
    const said = cut.firstWord !== null && cut.lastWord !== null
      ? words.slice(cut.firstWord, cut.lastWord + 1).map((word) => word.s).join(" ")
      : "";
    return (
      <div className="inspect">
        <header className="inspect-head">
          <span className={`tag tag-${cut.reason}`}>{REASON_LABEL[cut.reason] ?? "Tolto"}</span>
          <h2>{cut.removed.toFixed(1)}s fuori dal montaggio</h2>
        </header>
        <dl className="facts">
          <div><dt>Nel montaggio a</dt><dd>{formatTimecode(cut.at, fps)}</dd></div>
          <div><dt>Durata tolta</dt><dd>{cut.removed.toFixed(2)}s</dd></div>
        </dl>
        {said ? (
          <blockquote className="said">«{said}»</blockquote>
        ) : (
          <p className="hint">{cut.detail}</p>
        )}
        <div className="actions">
          <button className="act" onClick={onRestore}>Rimetti nel montaggio <kbd>R</kbd></button>
          {said && <button className="act ghost" onClick={onListen}>Ascolta com'era</button>}
        </div>
      </div>
    );
  }

  if (clip) {
    const said = words.slice(clip.first, clip.last + 1).map((word) => word.s).join(" ");
    return (
      <div className="inspect">
        <header className="inspect-head">
          <span className="tag tag-clip">Clip {clip.index + 1}</span>
          <h2>{clip.duration.toFixed(1)}s</h2>
        </header>
        <dl className="facts">
          <div><dt>Nel montaggio</dt><dd>{formatTimecode(clip.outputStart, fps)} → {formatTimecode(clip.outputEnd, fps)}</dd></div>
          <div><dt>Nel girato</dt><dd>{formatTimecode(clip.start, fps)} → {formatTimecode(clip.end, fps)}</dd></div>
          <div><dt>Parole</dt><dd>{clip.last - clip.first + 1}</dd></div>
        </dl>
        <blockquote className="said">«{said}»</blockquote>
        <div className="actions">
          <button className="act" onClick={onSplit}>Dividi al playhead <kbd>S</kbd></button>
          <button className="act ghost" onClick={onRemove}>Togli dal montaggio <kbd>⌫</kbd></button>
        </div>
      </div>
    );
  }

  return (
    <div className="inspect inspect-empty">
      <h2>Niente di selezionato</h2>
      <p className="hint">Clicca una clip per vederne i numeri e il testo, o un segno di taglio per rimettere quello che è stato tolto.</p>
      <ul className="teach">
        <li><b>Trascina i bordi</b> di una clip selezionata per allungarla o accorciarla. Si aggancia alle parole.</li>
        <li><b>Doppio clic</b> su una clip la divide nel punto in cui hai cliccato.</li>
        <li><b>Rotella</b> scorre, <b>⌘ + rotella</b> ingrandisce.</li>
        <li><b>Spazio</b> riproduce il montaggio saltando le parti tolte.</li>
      </ul>
    </div>
  );
}
