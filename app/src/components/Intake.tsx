import { useEffect, useRef, useState } from "react";
import { api, ApiError, type JobSummary, type ProjectSummary } from "../api";

interface Props {
  project: ProjectSummary;
  /** The project's recent jobs, newest first, from the editor's own polling. */
  jobs: JobSummary[];
  onUploaded: (project: ProjectSummary, job: JobSummary) => void;
  onRetry: () => void;
}

type Upload = { file: File; sent: number; total: number; startedAt: number; cancel: () => void };

/** Everything between an empty project and a cut to correct, on one screen:
 *  the place to drop the footage, how much of it has gone up, and then what
 *  the studio is doing with it — each step named, with how long it has taken
 *  so far, instead of one sentence that says the studio is working. */
export function Intake({ project, jobs, onUploaded, onRetry }: Props) {
  const [upload, setUpload] = useState<Upload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const now = useNow(1000);

  const start = (file: File) => {
    if (!file.type.startsWith("video/") && !/\.(mov|mp4|m4v|mkv|webm)$/i.test(file.name)) {
      setError("Questo non sembra un video: serve il file della camera (.mov o .mp4).");
      return;
    }
    setError(null);
    const { done, cancel } = api.uploadSource(project.id, file, (sent, total) =>
      setUpload((current) => (current ? { ...current, sent, total } : current)),
    );
    setUpload({ file, sent: 0, total: file.size, startedAt: Date.now(), cancel });
    done
      .then((result) => { setUpload(null); onUploaded(result.project, result.job); })
      .catch((cause) => {
        setUpload(null);
        setError(cause instanceof ApiError || cause instanceof Error ? cause.message : String(cause));
      });
  };

  // Leaving the page mid-upload throws the upload away: say so first.
  useEffect(() => {
    if (!upload) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [upload]);

  if (project.hasSource === false || upload) {
    if (upload) {
      const share = upload.total ? upload.sent / upload.total : 0;
      const seconds = (Date.now() - upload.startedAt) / 1000;
      const speed = seconds > 1 ? upload.sent / seconds : 0;
      const left = speed > 0 ? (upload.total - upload.sent) / speed : null;
      return (
        <div className="intake">
          <div className="intake-card">
            <p className="eyebrow">CARICO IL GIRATO</p>
            <h2>{upload.file.name}</h2>
            <div className="bar-track" aria-label="Avanzamento del caricamento">
              <div className="bar-fill" style={{ width: `${Math.round(share * 100)}%` }} />
            </div>
            <p className="intake-numbers">
              <b>{Math.round(share * 100)}%</b>
              <span>{mb(upload.sent)} di {mb(upload.total)}</span>
              {speed > 0 && <span>{mb(speed)}/s</span>}
              {left !== null && share < 1 && <span>ancora {duration(left)}</span>}
              {share >= 1 && <span>lo studio lo sta ricevendo…</span>}
            </p>
            <p className="hint">Tieni aperta questa pagina finché il caricamento non finisce: poi il resto lo fa lo studio, anche a pagina chiusa.</p>
            <button className="btn ghost" onClick={() => upload.cancel()}>Annulla</button>
          </div>
        </div>
      );
    }
    return (
      <div className="intake">
        <div
          className={over ? "dropzone over" : "dropzone"}
          onDragOver={(event) => { event.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(event) => {
            event.preventDefault();
            setOver(false);
            const file = event.dataTransfer.files?.[0];
            if (file) start(file);
          }}
          onClick={() => input.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") input.current?.click(); }}
        >
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M12 16V4M7 9l5-5 5 5" /><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
          </svg>
          <h2>Trascina qui il girato</h2>
          <p>oppure <u>scegli il file</u> · il video della camera così com'è, .mov o .mp4</p>
          <input
            ref={input}
            type="file"
            accept="video/*,.mov,.mp4,.m4v"
            hidden
            onChange={(event) => { const file = event.target.files?.[0]; if (file) start(file); event.target.value = ""; }}
          />
        </div>
        {error && <p className="intake-error">{error}</p>}
        <p className="hint intake-next">Dopo il caricamento lo studio ascolta il girato, sceglie i tagli e prepara l'anteprima. Poi qui si apre il montaggio da controllare.</p>
      </div>
    );
  }

  // The footage is in: the steps, from the jobs.
  const analyze = jobs.find((job) => job.kind === "analyze" || job.kind === "full");
  const proxy = jobs.find((job) => job.kind === "proxy");
  const failed = analyze?.status === "failed" || analyze?.status === "cancelled";
  const steps: { label: string; detail: string; state: "done" | "active" | "waiting" | "failed" }[] = [
    { label: "Girato caricato", detail: "", state: "done" },
    {
      label: analyze?.kind === "full" ? "Ascolto, taglio e render" : "Ascolto il girato e scelgo i tagli",
      detail: failed ? (analyze?.error ?? "non riuscito") : since(analyze, now),
      state: !analyze ? "waiting" : failed ? "failed" : analyze.status === "succeeded" ? "done" : analyze.status === "running" ? "active" : "waiting",
    },
    {
      label: "Preparo anteprima, forma d'onda e miniature",
      detail: since(proxy, now),
      state: !proxy ? "waiting" : proxy.status === "succeeded" ? "done" : proxy.status === "running" ? "active" : proxy.status === "failed" ? "failed" : "waiting",
    },
  ];
  return (
    <div className="intake">
      <div className="intake-card">
        <p className="eyebrow">LO STUDIO STA LAVORANDO</p>
        <h2>{project.name}</h2>
        <ol className="steps">
          {steps.map((step) => (
            <li key={step.label} className={step.state}>
              <i aria-hidden>{step.state === "done" ? "✓" : step.state === "failed" ? "!" : ""}</i>
              <span><b>{step.label}</b>{step.detail && <small>{step.detail}</small>}</span>
            </li>
          ))}
        </ol>
        {failed ? (
          <button className="btn primary" onClick={onRetry}>Riprova</button>
        ) : (
          <p className="hint">Per un girato di qualche minuto ci vogliono pochi minuti. Puoi chiudere la pagina: il lavoro continua, e al ritorno trovi il montaggio.</p>
        )}
      </div>
    </div>
  );
}

function useNow(every: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), every);
    return () => window.clearInterval(timer);
  }, [every]);
  return now;
}

function since(job: JobSummary | undefined, now: number): string {
  if (!job) return "";
  if (job.status === "queued") return "in coda";
  const from = job.startedAt ? Date.parse(job.startedAt) : NaN;
  const to = job.finishedAt ? Date.parse(job.finishedAt) : now;
  if (Number.isNaN(from)) return job.status === "running" ? "in corso" : "";
  const label = duration((to - from) / 1000);
  return job.status === "running" ? `in corso da ${label}` : `fatto in ${label}`;
}

function mb(bytes: number): string {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${Math.round(bytes / 1e6)} MB`;
}

function duration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  return `${minutes}m ${String(whole % 60).padStart(2, "0")}s`;
}
