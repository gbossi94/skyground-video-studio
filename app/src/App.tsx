import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type JobSummary, type ProjectSummary } from "./api";
import { Preview, type PreviewHandle } from "./components/Preview";
import { NewProject } from "./components/NewProject";
import { ENGINE, Questions, reviewQueue } from "./components/Questions";
import { Timeline, formatTime } from "./components/Timeline";
import type { CutPlan, CutState, Option, Question, Transcript } from "./types";

export default function App() {
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [creating, setCreating] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [working, setWorking] = useState<JobSummary | null>(null);
  const [cut, setCut] = useState<CutState | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [selected, setSelected] = useState<Question | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playingOption, setPlayingOption] = useState<string | null>(null);
  const player = useRef<PreviewHandle>(null);

  useEffect(() => {
    void (async () => {
      try {
        const projects = await api.projects();
        setProjects(projects);
        setLoaded(true);
        if (!projects.length) return;
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

  // The worker does the slow parts — transcription, the editor model, the
  // render — and the documents only change when it is done. Without this the
  // editor would show "the footage has not been heard yet" with a button
  // while a full job was already halfway through hearing it. Poll the queue,
  // show what is in flight, and reload the state when it lands.
  useEffect(() => {
    if (!project) return;
    let previous: JobSummary | null = null;
    let stopped = false;
    const tick = async () => {
      try {
        const jobs = await api.jobs(project.id);
        const live = jobs.find((job) => job.status === "queued" || job.status === "running") ?? null;
        if (!stopped) setWorking(live);
        if (previous && !live) {
          const ended = jobs.find((job) => job.id === previous?.id);
          if (ended?.status === "failed") setNotice(`${describe(ended.kind)}: fallito — ${ended.error ?? ""}`);
          else if (ended?.status === "succeeded") setNotice(`${describe(ended.kind)}: fatto`);
          await reload(project.id);
        }
        previous = live;
      } catch {
        /* the next tick retries */
      }
    };
    void tick();
    const timer = window.setInterval(tick, 5000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [project, reload]);

  // Keep the selection pointing at the freshest copy of the question: the plan
  // is rebuilt on every answer, so the old object is stale immediately.
  useEffect(() => {
    if (!cut?.plan) return setSelected(null);
    const queue = reviewQueue(cut.plan.questions);
    setSelected((current) => queue.find((q) => q.id === current?.id) ?? queue[0] ?? null);
  }, [cut]);

  // The sheet that lays out a project from a raw video has to be reachable
  // before any project exists: a new account starts with none, and the only
  // way to get one is this button.
  const sheet = creating && (
    <NewProject
      onClose={() => setCreating(false)}
      onCreated={(made) => {
        setProjects((current) => [made, ...current]);
        setCut(null);
        setProject(made);
      }}
    />
  );

  if (error) return <Empty title="Non riesco a mostrare il montaggio" detail={error} />;
  if (loaded && !project) {
    return (
      <>
        <Empty
          title="Nessun montaggio ancora"
          detail="Carica un video girato e lo studio lo trascrive, lo monta e lo rende da solo."
          action={{ label: "Nuovo montaggio", onClick: () => setCreating(true) }}
        />
        {sheet}
      </>
    );
  }
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
          {projects.length > 1 ? (
            <select
              className="picker"
              value={project.id}
              aria-label="Progetto"
              onChange={(event) => {
                const next = projects.find((item) => item.id === event.target.value);
                if (next) { setCut(null); setProject(next); }
              }}
            >
              {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          ) : (
            <h1>{project.name}</h1>
          )}
        </div>
        <div className="top-actions">
          {working && <span className="pill working">{describe(working.kind)}…</span>}
          <button className="ghost" onClick={() => setCreating(true)}>Nuovo montaggio</button>
          {plan && <Status plan={plan} />}
          <button
            className="ghost"
            disabled={busy !== null}
            title="Ricalcola tenendo le scelte fatte a mano"
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
            className="ghost"
            disabled={busy !== null || handAnswers(plan) === 0}
            title={
              handAnswers(plan) > 0
                ? `Butta via ${handAnswers(plan)} scelte fatte a mano e riparte dal girato`
                : "Non c'è nessuna scelta fatta a mano da buttare"
            }
            onClick={() =>
              run("propose", async () => {
                const result = await api.propose(project.id, false);
                setNotice("Ripartito dal girato: tutte le scelte sono di nuovo del motore.");
                setCut((current) =>
                  current ? { ...current, state: result.plan.status, plan: result.plan } : current,
                );
              })
            }
          >
            Riparti da zero
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
      {sheet}

      {working && !plan ? (
        <Empty
          title={`Lo studio sta lavorando: ${describe(working.kind)}`}
          detail={
            working.status === "queued"
              ? "In coda: parte appena il worker è libero."
              : "In corso nel worker. Questa pagina si aggiorna da sola quando ha finito."
          }
        />
      ) : !plan ? (
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
              ref={player}
              src={mediaUrl}
              plan={plan}
              playhead={playhead}
              playing={playing}
              onTime={setPlayhead}
              onPlayingChange={(value) => {
                setPlaying(value);
                if (!value) setPlayingOption(null);
              }}
            />
            <div className="transport">
              <button
                className="play"
                disabled={!mediaUrl}
                onClick={() => {
                  setPlayingOption(null);
                  player.current?.toggleEdit();
                }}
              >
                {playing && !playingOption ? "◼ Pausa" : "▶ Riproduci il montaggio"}
              </button>
              <span className="hint">Salta le parti rimosse · {formatTime(playhead)}</span>
            </div>
          </section>

          <Questions
            questions={plan.questions}
            selected={selected}
            busy={busy}
            playingOption={playingOption}
            onSelect={(question) => {
              setSelected(question);
              setPlayingOption(null);
              player.current?.stop();
              setPlayhead(question.at);
            }}
            onAnswer={answer}
            onListen={(question, option: Option) => {
              if (option.start === undefined || option.end === undefined) return;
              if (playingOption === option.id) {
                setPlayingOption(null);
                player.current?.stop();
                return;
              }
              setSelected(question);
              setPlayingOption(option.id);
              player.current?.playRange(option.start, option.end);
            }}
          />

          {transcript && (
            <Timeline
              plan={plan}
              transcript={transcript}
              playhead={playhead}
              onSeek={(at) => {
                setPlayingOption(null);
                player.current?.stop();
                setPlayhead(at);
              }}
              selectedQuestion={selected}
            />
          )}
        </main>
      )}
    </div>
  );
}

/** How many choices a person made by hand. Those are the sticky ones: a
 *  rebuild never overturns them, which is why there has to be a way out. */
function handAnswers(plan: CutPlan | null): number {
  if (!plan) return 0;
  return plan.questions.filter(
    (question) => question.answer !== null && question.answeredBy !== ENGINE,
  ).length;
}

function Status({ plan }: { plan: CutPlan }) {
  const removed = Math.round(plan.stats.removedShare * 100);
  const tone = plan.status === "ready" ? "ok" : plan.status === "applied" ? "done" : "draft";
  // "Pronto" on its own would hide the judgement calls the engine made to get
  // there. Say how many, so the number is an invitation to look at them.
  const byEngine = plan.questions.filter((question) => question.answeredBy === ENGINE).length;
  const state =
    plan.stats.openQuestions > 0
      ? `${plan.stats.openQuestions} da decidere`
      : byEngine > 0
        ? `pronto · ${byEngine} decise dal motore`
        : "pronto";
  return (
    <div className={`status ${tone}`}>
      <b>
        {formatTime(plan.stats.sourceDuration)} → {formatTime(plan.stats.outputDuration)}
      </b>
      <span>
        −{removed}% · {plan.stats.segments} segmenti · {state}
      </span>
    </div>
  );
}

/** What a job of this kind is doing, in the words of the person waiting. */
function describe(kind: string): string {
  switch (kind) {
    case "full": return "montaggio completo (trascrizione, montaggio, render)";
    case "analyze": return "trascrizione del girato";
    case "render": return "render del film";
    case "proxy": return "anteprima del girato";
    case "sync": return "sincronizzazione della composizione";
    case "validate": return "validazione del progetto";
    default: return kind;
  }
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
