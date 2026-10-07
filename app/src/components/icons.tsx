/** The toolbar's icons: drawn on a 16-unit grid with a 1.5 stroke, so they
 *  sit together at 16px and stay sharp on a Retina screen. */

export type IconName =
  | "undo" | "redo" | "split" | "trash" | "restore" | "magnet"
  | "help" | "minus" | "plus" | "fit" | "download" | "chevron";

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  const common = {
    width: size, height: size, viewBox: "0 0 16 16", fill: "none",
    stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round", strokeLinejoin: "round",
    "aria-hidden": true,
  } as const;
  switch (name) {
    case "undo":
      return <svg {...common}><path d="M5.5 3.5 2.5 6.5l3 3" /><path d="M2.5 6.5h7a4 4 0 0 1 0 8H7" /></svg>;
    case "redo":
      return <svg {...common}><path d="m10.5 3.5 3 3-3 3" /><path d="M13.5 6.5h-7a4 4 0 0 0 0 8H9" /></svg>;
    case "split":
      return <svg {...common}><path d="M8 1.5v13" /><rect x="1.5" y="4.5" width="4.5" height="7" rx="1" /><rect x="10" y="4.5" width="4.5" height="7" rx="1" /></svg>;
    case "trash":
      return <svg {...common}><path d="M2.5 4.5h11" /><path d="M6 4.5V3a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1.5" /><path d="m3.8 4.5.7 9a1 1 0 0 0 1 .9h5a1 1 0 0 0 1-.9l.7-9" /></svg>;
    case "restore":
      return <svg {...common}><path d="M2 8a6 6 0 1 0 1.8-4.3" /><path d="M2 2.5v3.2h3.2" /><path d="M8 5v3l2 1.5" /></svg>;
    case "magnet":
      return <svg {...common}><path d="M3.5 2.5v5.5a4.5 4.5 0 0 0 9 0V2.5" /><path d="M3.5 5.5h3M9.5 5.5h3" /><path d="M6.5 2.5v5.5a1.5 1.5 0 0 0 3 0V2.5" /></svg>;
    case "help":
      return <svg {...common}><circle cx="8" cy="8" r="6.5" /><path d="M6.2 6.2a1.9 1.9 0 1 1 2.6 1.8c-.5.2-.8.6-.8 1.1v.4" /><path d="M8 11.6h.01" /></svg>;
    case "minus":
      return <svg {...common}><path d="M3.5 8h9" /></svg>;
    case "plus":
      return <svg {...common}><path d="M3.5 8h9M8 3.5v9" /></svg>;
    case "fit":
      return <svg {...common}><path d="M2 5.5v-3h3M11 2.5h3v3M14 10.5v3h-3M5 13.5H2v-3" /><path d="M5 8h6" /></svg>;
    case "download":
      return <svg {...common}><path d="M8 2v8.5M4.5 7 8 10.5 11.5 7" /><path d="M2.5 13.5h11" /></svg>;
    case "chevron":
      return <svg {...common}><path d="m4.5 6.5 3.5 3.5 3.5-3.5" /></svg>;
  }
}
