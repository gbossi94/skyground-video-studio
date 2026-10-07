import { useEffect, useState } from "react";
import { api, type Invitation, type Me } from "../api";

/** Who can get in. An administrator invites by email and gets a link that
 *  works once — the person chooses their own password — and everybody can
 *  change their own password here. */
export function People({ me, onClose }: { me: Me; onClose: () => void }) {
  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div className="sheet people-sheet" role="dialog" aria-label="Persone">
        <header className="decide-head">
          <div>
            <p className="eyebrow">PERSONE</p>
            <h2>{me.name || me.email}</h2>
            <p className="hint">{me.email}{me.isAdmin ? " · amministratore" : ""}</p>
          </div>
          <button className="ghost small" onClick={onClose} aria-label="Chiudi">✕</button>
        </header>
        {me.isAdmin && <Invite />}
        <ChangePassword />
      </div>
    </>
  );
}

function Invite() {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [admin, setAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [made, setMade] = useState<{ email: string; url: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [pending, setPending] = useState<Invitation[]>([]);

  const refresh = () => { api.invitations().then(setPending).catch(() => undefined); };
  useEffect(refresh, []);

  const invite = async () => {
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const result = await api.invite(email.trim(), name.trim(), admin);
      setMade({ email: result.email, url: result.url });
      setEmail(""); setName(""); setAdmin(false);
      refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="people-block">
      <h3>Invita qualcuno</h3>
      <form className="new-form" onSubmit={(event) => { event.preventDefault(); void invite(); }}>
        <label>
          Email
          <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="nome@skyground.online" />
        </label>
        <label>
          Nome
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Facoltativo" />
        </label>
        <label className="check">
          <input type="checkbox" checked={admin} onChange={(event) => setAdmin(event.target.checked)} />
          <span>Amministratore: vede tutti i progetti e può invitare altre persone</span>
        </label>
        {error && <p className="intake-error">{error}</p>}
        <button className="primary" type="submit" disabled={busy || !email.includes("@")}>
          {busy ? "Creo l'invito…" : "Crea il link d'invito"}
        </button>
      </form>

      {made && (
        <div className="invite-link">
          <p className="hint">Link per <b>{made.email}</b>. Si vede solo adesso: copialo e mandaglielo su un canale privato. Vale una volta, per 7 giorni.</p>
          <div className="copy-row">
            <input readOnly value={made.url} onFocus={(event) => event.currentTarget.select()} />
            <button
              className="btn tiny"
              onClick={() => {
                void navigator.clipboard?.writeText(made.url).then(() => setCopied(true)).catch(() => undefined);
              }}
            >
              {copied ? "Copiato" : "Copia"}
            </button>
          </div>
        </div>
      )}

      {pending.length > 0 && (
        <ul className="pending">
          {pending.map((item) => (
            <li key={item.id}>
              <span>
                <b>{item.name || item.email}</b>
                <small>{item.email}{item.admin ? " · amministratore" : ""} · scade il {new Date(item.expiresAt).toLocaleDateString("it-IT")}</small>
              </span>
              <button className="btn tiny ghost" onClick={() => { void api.revokeInvitation(item.id).then(refresh); }}>Annulla</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const change = async () => {
    if (next !== again) { setError("Le due password nuove non coincidono."); return; }
    setBusy(true);
    setError(null);
    try {
      await api.changePassword(current, next);
      setDone(true);
      // Changing the password signs out every browser, this one included.
      window.setTimeout(() => window.location.replace("/"), 1500);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="people-block">
      <h3>La tua password</h3>
      {done ? (
        <p className="hint">Password cambiata. Rientra con quella nuova.</p>
      ) : (
        <form className="new-form" onSubmit={(event) => { event.preventDefault(); void change(); }}>
          <label>
            Password attuale
            <input type="password" value={current} onChange={(event) => setCurrent(event.target.value)} autoComplete="current-password" />
          </label>
          <label>
            Nuova password
            <input type="password" value={next} onChange={(event) => setNext(event.target.value)} autoComplete="new-password" />
          </label>
          <label>
            Ripeti la nuova
            <input type="password" value={again} onChange={(event) => setAgain(event.target.value)} autoComplete="new-password" />
          </label>
          {error && <p className="intake-error">{error}</p>}
          <button className="ghost" type="submit" disabled={busy || !current || !next || !again}>
            {busy ? "Cambio…" : "Cambia password"}
          </button>
          <p className="hint">Almeno 12 caratteri. Dopo il cambio si esce da tutti i dispositivi.</p>
        </form>
      )}
    </section>
  );
}
