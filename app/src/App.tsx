import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type ProjectSummary } from "./api";
import { Preview } from "./components/Preview";
import { Questions } from "./components/Questions";
import { Timeline, formatTime } from "./components/Timeline";
import type { CutPlan, CutState, Question, Transcript } from "./types";

export default function App() {
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [cut, setCut] = useState<CutState | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [selected, setSelected] = useState<Question | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const projects = await api.projects();
        if (!projects.length) return setError("Nessun progetto disponibile");
        setProject(projects[0]);
      } catch (cause) {
        // Without a session there is nothing to show here, and the sign-in form
        // — which on a fresh studio is the form that creates the first account
        // — lives on the panel at the root. Send the visitor there instead of
        // leaving them on a dead end.
        if (cause instanceof ApiError && cause.status === 401) {
          window.location.replace("/");
          return;
        }
        setError(String(cause));
      }
    })();
  }, []);

  const reload = useCallback(async (slug: string) => {
    const state = await api.cut(slug);
    setCut(state);
    if (state.analysis) {
      setTranscript(await api.transcript(slug));
    }
    return state;
  }, []);

  useEffect(() => {
    if (!project) return;
    void reload(project.id).catch((cause) => setError(String(cause)));
  }, [project, reload]);

  // Keep the selection pointing at the freshest copy of the question: the plan
  // is rebuilt on every answer, so the old object is stale immediately.
  useEffect(() => {
    if (!cut?.plan) return setSelected(null);
    const open = cut.plan.questions.filter((question) => !question.resolved);
    setSelected((current) => open.find((q) => q.id === current?.id) ?? open[0] ?? null);
  }, [cut]);

  if (error) return <Empty title="Non riesco a mostrare il montaggio" detail={error} />;
  if (!project || !cut) return <Empty title="Carico il progetto…" />;

  const plan = cut.plan;
  const source = plan?.source ?? "assets/raw.mov";
  // The proxy when it exists, the original only as a fallback.
  const mediaUrl = cut.analysis?.proxyUrl ?? `/media/${project.id}/${source}`;

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setNotice(null);
    try {
      await action();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  };

  const answer = (question: Question, option: string) =>
    run(question.id, async () => {
      const result = await api.answer(project.id, question.id, option);
      setCut((current) => (current ? { ...current, state: result.plan.status, plan: result.plan } : current));
    });

  return (
    <div className="app">
      <header className="top">
        <div>
          <p className="eyebrow">MONTAGGIO AUTOMATICO</p>
          <h1>{project.name}</h1>
        </div>
        <div className="top-actions">
          {plan && <Status plan={plan} />}
          <button
            className="ghost"
            disabled={busy !== null}
            onClick={() =>
              run("propose", async () => {
                const result = await api.propose(project.id);
                setCut((current) =>
                  current ? { ...current, state: result.plan.status, plan: result.plan } : current,
                );
              })
            }
          >
            Rigenera proposta
          </button>
          <button
            className="primary"
            disabled={busy !== null || !plan || plan.stats.openQuestions > 0}
            title={
              plan && plan.stats.openQuestions > 0
                ? "Restano decisioni aperte"
                : "Scrive la timeline del progetto"
            }
            onClick={() =>
              run("apply", async () => {
                const result = await api.apply(project.id);
                setNotice(
                  `Montaggio applicato: ${result.clips} clip, ${result.duration.toFixed(1)}s` +
                    (result.problems.length ? ` · ${result.problems.length} problemi da guardare` : ""),
                );
                await reload(project.id);
              })
            }
          >
            Applica al progetto
          </button>
        </div>
      </header>

      {notice && <div className="notice">{notice}</div>}

      {!plan ? (
        <Missing
          state={cut.state}
          busy={busy !== null}
          onAnalyze={() => run("analyze", () => api.analyze(project.id))}
          onPropose={() =>
            run("propose", async () => {
              const result = await api.propose(project.id);
              setCut((current) =>
                current ? { ...current, state: result.plan.status, plan: result.plan } : current,
              );
            })
          }
        />
      ) : (
        <main className="workspace">
          <section className="stage">
            <Preview
              src={mediaUrl}
              plan={plan}
              playhead={playhead}
              playing={playing}
              onTime={setPlayhead}
              onPlayingChange={setPlaying}
            />
            {transcript && (
              <Timeline
                plan={plan}
                transcript={transcript}
                playhead={playhead}
                onSeek={(at) => {
                  setPlaying(false);
                  setPlayhead(at);
                }}
                selectedQuestion={selected}
              />
            )}
          </section>
          <Questions
            questions={plan.questions}
            selected={selected}
            busy={busy}
            onSelect={(question) => {
              setSelected(question);
              setPlaying(false);
              setPlayhead(question.at);
            }}
            onAnswer={answer}
          />
        </main>
      )}
    </div>
  );
}

function Status({ plan }: { plan: CutPlan }) {
  const removed = Math.round(plan.stats.removedShare * 100);
  const tone = plan.status === "ready" ? "ok" : plan.status === "applied" ? "done" : "draft";
  return (
    <div className={`status ${tone}`}>
      <b>
        {formatTime(plan.stats.sourceDuration)} → {formatTime(plan.stats.outputDuration)}
      </b>
      <span>
        −{removed}% · {plan.stats.segments} segmenti ·{" "}
        {plan.stats.openQuestions > 0 ? `${plan.stats.openQuestions} da decidere` : "pronto"}
      </span>
    </div>
  );
}

function Missing({
  state,
  busy,
  onAnalyze,
  onPropose,
}: {
  state: string;
  busy: boolean;
  onAnalyze: () => void;
  onPropose: () => void;
}) {
  if (state === "senza-analisi") {
    return (
      <Empty
        title="Il girato non è ancora stato ascoltato"
        detail="La trascrizione gira nel worker: è il passo lento, si fa una volta sola."
        action={{ label: busy ? "In coda…" : "Analizza il girato", onClick: onAnalyze, disabled: busy }}
      />
    );
  }
  return (
    <Empty
      title="Analisi pronta, nessuna proposta"
      detail="Il motore può proporre un montaggio senza pause e senza ripartenze."
      action={{ label: busy ? "Calcolo…" : "Proponi il montaggio", onClick: onPropose, disabled: busy }}
    />
  );
}

function Empty({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: { label: string; onClick: () => void; disabled?: boolean };
}) {
  return (
    <div className="empty">
      <h2>{title}</h2>
      {detail && <p>{detail}</p>}
      {action && (
        <button className="primary" onClick={action.onClick} disabled={action.disabled}>
          {action.label}
        </button>
      )}
    </div>
  );
}
