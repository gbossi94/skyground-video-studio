import { useEffect, useState } from "react";
import { api } from "../api";

/** Where an invitation link lands: whose it is, a name and a password chosen
 *  by the person themselves, and then straight into the studio. */
export function AcceptInvite({ token, onDone }: { token: string; onDone: () => void }) {
  const [invite, setInvite] = useState<{ email: string; name: string } | null>(null);
  const [invalid, setInvalid] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.openInvitation(token)
      .then((found) => { setInvite(found); setName(found.name); })
      .catch((cause) => setInvalid(cause instanceof Error ? cause.message : String(cause)));
  }, [token]);

  const submit = async () => {
    if (password !== again) { setError("Le due password non coincidono."); return; }
    setBusy(true);
    setError(null);
    try {
      await api.acceptInvitation(token, password, name.trim());
      onDone();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setBusy(false);
    }
  };

  return (
    <div className="blank">
      <div className="invite-card">
        <span className="mark" aria-hidden>S</span>
        {invalid ? (
          <>
            <h2>Questo invito non vale più</h2>
            <p className="hint">{invalid}</p>
          </>
        ) : !invite ? (
          <p className="hint">Apro l'invito…</p>
        ) : (
          <form className="new-form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            <h2>Benvenuto in Skyground Studio</h2>
            <p className="hint">Invito per <b>{invite.email}</b>. Scegli come comparire e la tua password: la conosci solo tu.</p>
            <label>
              Nome
              <input value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" />
            </label>
            <label>
              Password
              <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" autoFocus />
            </label>
            <label>
              Ripeti la password
              <input type="password" value={again} onChange={(event) => setAgain(event.target.value)} autoComplete="new-password" />
            </label>
            {error && <p className="intake-error">{error}</p>}
            <button className="primary" type="submit" disabled={busy || !password || !again}>
              {busy ? "Creo l'account…" : "Entra nello studio"}
            </button>
            <p className="hint">Almeno 12 caratteri. L'invito vale una volta sola.</p>
          </form>
        )}
      </div>
    </div>
  );
}
