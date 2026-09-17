import { SHORTCUTS } from "../edit/keys";

/** The keyboard, on one card. Opened with `?`, closed with Escape. */
export function ShortcutsHelp({ onClose }: { onClose: () => void }) {
  return (
    <div className="help-backdrop" onClick={onClose} role="presentation">
      <div className="help" onClick={(event) => event.stopPropagation()} role="dialog" aria-label="Scorciatoie">
        <header>
          <p className="eyebrow">TASTIERA</p>
          <button className="ghost small" onClick={onClose} aria-label="Chiudi">✕</button>
        </header>
        <table>
          <tbody>
            {SHORTCUTS.map((item) => (
              <tr key={item.action}>
                <td><kbd>{item.keys}</kbd></td>
                <td>{item.what}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="hint">
          Sulla timeline: trascina il bordo di un pezzo per spostare il taglio (si aggancia alle
          parole e ai silenzi), doppio clic su un pezzo per dividerlo, rotella per scorrere,
          ⌘ + rotella per lo zoom.
        </p>
      </div>
    </div>
  );
}
