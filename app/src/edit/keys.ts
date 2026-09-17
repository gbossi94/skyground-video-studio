/** The keyboard, as an editor expects it.
 *
 *  J/K/L and I/O are the language every cutting tool speaks; the rest is what
 *  the hands reach for without looking. Nothing fires while a text field has
 *  focus, and Cmd is the modifier on the Mac this is built for.
 */

export type Action =
  | "play-toggle"
  | "shuttle-back"
  | "pause"
  | "shuttle-forward"
  | "frame-back"
  | "frame-forward"
  | "frames-back"
  | "frames-forward"
  | "home"
  | "end"
  | "previous-boundary"
  | "next-boundary"
  | "select-previous"
  | "select-next"
  | "trim-in"
  | "trim-out"
  | "split"
  | "remove"
  | "restore"
  | "undo"
  | "redo"
  | "zoom-in"
  | "zoom-out"
  | "zoom-fit"
  | "save"
  | "apply"
  | "help"
  | "escape";

export interface Shortcut {
  action: Action;
  keys: string;
  what: string;
}

/** One line per action, in the order the help shows them. */
export const SHORTCUTS: Shortcut[] = [
  { action: "play-toggle", keys: "Spazio", what: "riproduci / pausa il montaggio" },
  { action: "shuttle-back", keys: "J", what: "indietro (di nuovo: più veloce)" },
  { action: "pause", keys: "K", what: "fermo" },
  { action: "shuttle-forward", keys: "L", what: "avanti (di nuovo: più veloce)" },
  { action: "frame-back", keys: "←", what: "un fotogramma indietro" },
  { action: "frame-forward", keys: "→", what: "un fotogramma avanti" },
  { action: "frames-back", keys: "⇧←", what: "dieci fotogrammi indietro" },
  { action: "frames-forward", keys: "⇧→", what: "dieci fotogrammi avanti" },
  { action: "home", keys: "Home", what: "inizio" },
  { action: "end", keys: "End", what: "fine" },
  { action: "previous-boundary", keys: "[", what: "taglio precedente" },
  { action: "next-boundary", keys: "]", what: "taglio successivo" },
  { action: "select-previous", keys: "↑", what: "seleziona la clip precedente" },
  { action: "select-next", keys: "↓", what: "seleziona la clip successiva" },
  { action: "trim-in", keys: "I", what: "l'inizio della clip qui" },
  { action: "trim-out", keys: "O", what: "la fine della clip qui" },
  { action: "split", keys: "S", what: "dividi la clip al playhead" },
  { action: "remove", keys: "⌫", what: "togli la clip selezionata" },
  { action: "restore", keys: "R", what: "rimetti la parte tolta" },
  { action: "undo", keys: "⌘Z", what: "annulla" },
  { action: "redo", keys: "⇧⌘Z", what: "ripeti" },
  { action: "zoom-in", keys: "+", what: "avvicina" },
  { action: "zoom-out", keys: "−", what: "allontana" },
  { action: "zoom-fit", keys: "0", what: "tutto il montaggio" },
  { action: "save", keys: "⌘S", what: "salva adesso" },
  { action: "apply", keys: "⌘↵", what: "applica e rigenera" },
  { action: "help", keys: "?", what: "questa tabella" },
];

/** The action for a key event, or null when the event is not ours. */
export function actionFor(event: KeyboardEvent): Action | null {
  const target = event.target as HTMLElement | null;
  const tag = target?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable) {
    return null;
  }
  const meta = event.metaKey || event.ctrlKey;
  const key = event.key;

  if (meta) {
    if (key === "z" || key === "Z") return event.shiftKey ? "redo" : "undo";
    if (key === "y" || key === "Y") return "redo";
    if (key === "s" || key === "S") return "save";
    if (key === "Enter") return "apply";
    return null;
  }
  switch (key) {
    case " ": return "play-toggle";
    case "j": case "J": return "shuttle-back";
    case "k": case "K": return "pause";
    case "l": case "L": return "shuttle-forward";
    case "ArrowLeft": return event.shiftKey ? "frames-back" : "frame-back";
    case "ArrowRight": return event.shiftKey ? "frames-forward" : "frame-forward";
    case "ArrowUp": return "select-previous";
    case "ArrowDown": return "select-next";
    case "Home": return "home";
    case "End": return "end";
    case "[": return "previous-boundary";
    case "]": return "next-boundary";
    case "i": case "I": return "trim-in";
    case "o": case "O": return "trim-out";
    case "s": case "S": return "split";
    case "Backspace": case "Delete": return "remove";
    case "r": case "R": return "restore";
    case "z": return "undo";
    case "y": case "Z": return "redo";
    case "+": case "=": return "zoom-in";
    case "-": case "_": return "zoom-out";
    case "0": return "zoom-fit";
    case "?": return "help";
    case "Escape": return "escape";
    default: return null;
  }
}
