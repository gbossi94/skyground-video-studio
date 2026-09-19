import { useState } from "react";
import { api, type ProjectSummary } from "../api";

interface Props {
  onCreated: (project: ProjectSummary) => void;
  onClose: () => void;
}

/** A new film starts with a name and nothing else. The project opens at
 *  once, and the footage goes in from inside it, where the upload and every
 *  step after it can be watched. */
export function NewProject({ onCreated, onClose }: Props) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const project = await api.createEmpty(name.trim());
      onCreated(project);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setBusy(false);
    }
  };

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div className="sheet new-sheet" role="dialog" aria-label="Nuovo video">
        <header className="decide-head">
          <div>
            <p className="eyebrow">NUOVO VIDEO</p>
            <h2>Come si chiama?</h2>
          </div>
          <button className="ghost small" onClick={onClose} aria-label="Chiudi">✕</button>
        </header>
        <form className="new-form" onSubmit={(event) => { event.preventDefault(); void create(); }}>
          <label>
            Nome del video
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Crescita centri estetici 03"
              autoFocus
            />
          </label>
          {error && <p className="intake-error">{error}</p>}
          <button className="primary" type="submit" disabled={!name.trim() || busy}>
            {busy ? "Creo…" : "Crea e apri"}
          </button>
          <p className="hint">Il girato lo carichi subito dopo, dentro il progetto.</p>
        </form>
      </div>
    </>
  );
}
