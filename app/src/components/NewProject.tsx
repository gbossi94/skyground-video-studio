import { useEffect, useRef, useState } from "react";
import { api, type JobSummary, type ProjectSummary } from "../api";

interface Props {
  onCreated: (project: ProjectSummary) => void;
  onClose: () => void;
}

/** From a file on your machine to a film, with nothing to do in between.
 *
 *  The file goes up as the body of one request and the studio lays a project
 *  out around it; then one job runs the whole chain — hear, decide, cut,
 *  rebuild, render, publish — and its status is shown here until it is done. */
export function NewProject({ onCreated, onClose }: Props) {
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [stage, setStage] = useState<"form" | "uploading" | "queued" | "done" | "failed">("form");
  const [job, setJob] = useState<JobSummary | null>(null);
  const [slug, setSlug] = useState("");
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current); }, []);

  const start = async () => {
    if (!file || !name.trim()) return;
    const id = slugify(name);
    setSlug(id);
    setStage("uploading");
    setError(null);
    try {
      const project = await api.createProject(id, name.trim(), file, "beauty-centers-growth-01");
      onCreated(project);
      const queued = await api.fullCut(id);
      setJob(queued);
      setStage("queued");
      timer.current = window.setInterval(async () => {
        const latest = await api.job(id, queued.id);
        setJob(latest);
        if (latest.status === "succeeded") { setStage("done"); window.clearInterval(timer.current!); }
        if (latest.status === "failed" || latest.status === "cancelled") {
          setStage("failed"); window.clearInterval(timer.current!);
        }
      }, 5000);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setStage("failed");
    }
  };

  const report = job?.result as { editor?: { model?: string; segments?: number; duration?: number; cuts?: number; summary?: string }; render?: { output?: string } } | null;

  return (
    <div className="sheet" role="dialog" aria-label="Nuovo montaggio">
      <header className="decide-head">
        <div>
          <p className="eyebrow">NUOVO MONTAGGIO</p>
          <h2>{stage === "form" ? "Dammi il girato" : name}</h2>
        </div>
        <button className="ghost small" onClick={onClose} aria-label="Chiudi">✕</button>
      </header>

      {stage === "form" && (
        <form className="new-form" onSubmit={(event) => { event.preventDefault(); void start(); }}>
          <label>
            Nome del video
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Crescita centri estetici 02" autoFocus />
          </label>
          <label>
            File grezzo
            <input type="file" accept="video/*,.mov,.mp4" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </label>
          <p className="hint">
            Il file sale così com'è. Poi il montatore legge tutto il girato, decide cosa resta con un motivo per ogni taglio, e il film viene ricostruito, sottotitolato e renderizzato. Torna qui quando è pronto.
          </p>
          <button className="primary" type="submit" disabled={!file || !name.trim()}>
            Carica e monta
          </button>
        </form>
      )}

      {stage === "uploading" && <p className="hint">Carico {file?.name} ({Math.round((file?.size ?? 0) / 1048576)} MB)…</p>}

      {(stage === "queued" || stage === "done" || stage === "failed") && job && (
        <div className="job">
          <p className="decide-meta"><span className="kind">{job.status}</span><span className="at">{slug}</span></p>
          {stage === "queued" && <p className="hint">Ascolto, montaggio, ricostruzione e render: da dieci a venti minuti. Puoi chiudere questa finestra, il lavoro continua.</p>}
          {stage === "done" && report?.editor && (
            <p className="context">
              Montato da {report.editor.model}: {report.editor.segments} clip, {Math.round(report.editor.duration ?? 0)}s, {report.editor.cuts} tagli.
              {report.editor.summary ? ` ${report.editor.summary}` : ""}
              {report.render?.output ? ` Render: ${report.render.output}.` : ""}
            </p>
          )}
          {stage === "failed" && <p className="context">{job.error ?? error ?? "non riuscito"}</p>}
        </div>
      )}
      {stage === "failed" && !job && <p className="context">{error}</p>}
    </div>
  );
}

function slugify(name: string): string {
  const base = name.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "video";
  const stamp = new Date().toISOString().slice(2, 10).replace(/-/g, "");
  return `${base}-${stamp}`;
}
