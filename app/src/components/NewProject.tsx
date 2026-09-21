import { useState } from "react";
import { api, type ProjectSummary } from "../api";

interface Props {
  /** The project being left, when there is one: a new video replaces it on
   *  screen, so it is said before anything is created. */
  leaving?: { name: string; dirty: boolean; onSave: () => Promise<unknown> } | null;
  onCreated: (project: ProjectSummary) => void;
  onClose: () => void;
}

/** The one way into a new film, whether it was asked for from the button or
 *  from the project menu: say what is being left, take a name, open it. The
 *  footage goes in from inside the project, where it can be watched. */
export function NewProject({ leaving, onCreated, onClose }: Props) {
  const [step, setStep] = useState<"leave" | "name">(leaving ? "leave" : "name");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const guard = async (save: boolean) => {
    if (busy) return;
    setError(null);
    if (save && leaving) {
      setBusy("save");
      try {
        await leaving.onSave();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
        setBusy(null);
        return;
      }
      setBusy(null);
    }
    setStep("name");
  };

  const create = async () => {
    if (!name.trim() || busy) return;
    setBusy("create");
    setError(null);
    try {
      const project = await api.createEmpty(name.trim());
      onCreated(project);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setBusy(null);
    }
  };

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div className="sheet new-sheet" role="dialog" aria-label="Nuovo video">
        <header className="decide-head">
          <div>
            <p className="eyebrow">NUOVO VIDEO</p>
            <h2>{step === "leave" ? "Lasci il montaggio aperto" : "Come si chiama?"}</h2>
          </div>
          <button className="ghost small" onClick={onClose} aria-label="Chiudi">✕</button>
        </header>

        {step === "leave" && leaving && (
          <div className="new-form">
            <p className="context">
              Un nuovo video prende il posto di <b>{leaving.name}</b> su questo schermo.
              {leaving.dirty
                ? " Le correzioni che hai fatto non sono ancora salvate."
                : " Il suo montaggio è salvato: lo ritrovi dalla tendina dei progetti quando vuoi."}
            </p>
            {error && <p className="intake-error">{error}</p>}
            <div className="new-actions">
              {leaving.dirty ? (
                <>
                  <button className="primary" onClick={() => void guard(true)} disabled={busy !== null}>
                    {busy === "save" ? "Salvo…" : "Salva e continua"}
                  </button>
                  <button className="ghost" onClick={() => void guard(false)} disabled={busy !== null}>
                    Esci senza salvare
                  </button>
                </>
              ) : (
                <button className="primary" onClick={() => void guard(false)}>Continua</button>
              )}
              <button className="ghost" onClick={onClose} disabled={busy !== null}>Resta qui</button>
            </div>
          </div>
        )}

        {step === "name" && (
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
            <button className="primary" type="submit" disabled={!name.trim() || busy !== null}>
              {busy === "create" ? "Creo…" : "Crea e apri"}
            </button>
            <p className="hint">Il girato lo carichi subito dopo, dentro il progetto.</p>
          </form>
        )}
      </div>
    </>
  );
}
