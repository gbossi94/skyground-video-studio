import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type ConflictPayload, type JobSummary, type ProjectSummary } from "./api";
import { EditorTimeline, type Selection, type TimelineHandle } from "./components/EditorTimeline";
import { NewProject } from "./components/NewProject";
import { Player, type PlayerHandle } from "./components/Player";
import { ENGINE, Questions, reviewQueue } from "./components/Questions";
import { ShortcutsHelp } from "./components/ShortcutsHelp";
import { formatTime, formatTimecode } from "./components/timeline/time";
import { actionFor, type Action } from "./edit/keys";
import {
  boundaries,
  commit,
  fromPlan,
  gapAt,
  historyOf,
  lastWordUpTo,
  rangeAt,
  redo,
  removeRange,
  restoreGap,
  rulesOf,
  sameCut,
  segmentsOf,
  splitRange,
  toRequest,
  trimToPlayhead,
  undo,
  type EditState,
  type History,
} from "./edit/model";
import type { CutPlan, CutState, Option, Question, Transcript } from "./types";

type SaveState = "salvato" | "non salvato" | "salvataggio…" | "conflitto";

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
  const [help, setHelp] = useState(false);

  // The edit: what is on the timeline, its history, and what the server has.
  const [history, setHistory] = useState<History | null>(null);
  const [baseline, setBaseline] = useState<EditState | null>(null);
  const [preview, setPreview] = useState<EditState | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("salvato");
  const [conflict, setConflict] = useState<ConflictPayload | null>(null);

  const player = useRef<PlayerHandle>(null);
  const timeline = useRef<TimelineHandle>(null);
  const segmentsRef = useRef<{ start: number; end: number }[]>([]);
  const playheadRef = useRef(0);
  playheadRef.current = playhead;

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
    setHistory(null);
    setBaseline(null);
    setSelection(null);
    setConflict(null);
    void reload(project.id).catch((cause) => setError(String(cause)));
  }, [project, reload]);

  // The plan from the server is the baseline of the edit. When it changes —
  // loaded, saved, answered, regenerated — the timeline starts from it again,
  // unless the person is mid-edit on the same plan (then the history stays).
  useEffect(() => {
    if (!cut?.plan || !transcript) return;
    const fresh = fromPlan(cut.plan, transcript.words);
    setBaseline(fresh);
    setHistory((current) => {
      if (current && baseline && sameCut(current.present, baseline)) return historyOf(fresh);
      if (current && baseline && !sameCut(fresh, baseline)) return commit(current, "dal server", fresh);
      return current ?? historyOf(fresh);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cut?.plan, transcript]);

  const state = history?.present ?? null;
  const dirty = !!(state && baseline && !sameCut(state, baseline));
  useEffect(() => {
    setSaveState((current) => (current === "conflitto" ? current : dirty ? "non salvato" : "salvato"));
  }, [dirty]);
  segmentsRef.current = state ? segmentsOf(preview ?? state) : [];

  // The worker does the slow parts — transcription, the editor model, the
  // render — and the documents only change when it is done. Poll the queue,
  // show what is in flight, and reload the state when it lands.
  useEffect(() => {
    if (!project) return;
    let previous: JobSummary | null = null;
    let stopped = false;
    let timer = 0;
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
        if (!stopped) timer = window.setTimeout(tick, live ? 2000 : 5000);
      } catch {
        if (!stopped) timer = window.setTimeout(tick, 5000);
      }
    };
    void tick();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [project, reload]);

  // Keep the selection pointing at the freshest copy of the question: the plan
  // is rebuilt on every answer, so the old object is stale immediately.
  useEffect(() => {
    if (!cut?.plan) return setSelected(null);
    const queue = reviewQueue(cut.plan.questions);
    setSelected((current) => queue.find((q) => q.id === current?.id) ?? queue[0] ?? null);
  }, [cut]);

  const plan = cut?.plan ?? null;
  const duration = transcript?.duration ?? plan?.sourceDuration ?? 0;
  const fps = cut?.media?.fps ?? project?.canvas.fps ?? 30;
  const rules = useMemo(() => (plan ? rulesOf(plan, duration) : null), [plan, duration]);
  const words = transcript?.words ?? [];

  // ------------------------------------------------------------- commands

  const apply = useCallback((label: string, next: EditState | null) => {
    if (!next) return;
    setHistory((current) => (current ? commit(current, label, next) : current));
    setSelection(null);
  }, []);

  const seekTo = useCallback((seconds: number) => {
    setPlayingOption(null);
    player.current?.seek(seconds);
    setPlayhead(seconds);
  }, []);

  const save = useCallback(async () => {
    if (!project || !state || !cut) return null;
    setSaveState("salvataggio…");
    try {
      const result = await api.edits(project.id, toRequest(state), cut.etag ?? null);
      setCut((current) => (current ? { ...current, state: result.plan.status, plan: result.plan, etag: result.etag } : current));
      setConflict(null);
      setSaveState("salvato");
      if (result.notes.length) setNotice(result.notes.join(" · "));
      return result;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(cause.payload as ConflictPayload);
        setSaveState("conflitto");
      } else {
        setSaveState("non salvato");
        setNotice(cause instanceof Error ? cause.message : String(cause));
      }
      return null;
    }
  }, [project, state, cut]);

  const applyAndRebuild = useCallback(async () => {
    if (!project || !plan) return;
    setBusy("apply");
    setNotice(null);
    try {
      let etag = cut?.etag ?? null;
      if (dirty) {
        const saved = await save();
        if (!saved) return;
        etag = saved.etag;
      }
      const result = await api.apply(project.id, { etag, rebuild: true, render: true });
      setNotice(
        `Montaggio applicato (revisione ${result.revision}): ${result.clips} clip, ${result.duration.toFixed(1)}s` +
          (result.job ? " · il worker rigenera video, sottotitoli e render" : "") +
          (result.problems.length ? ` · ${result.problems.length} problemi da guardare` : ""),
      );
      await reload(project.id);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(cause.payload as ConflictPayload);
        setSaveState("conflitto");
      } else setNotice(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }, [project, plan, cut?.etag, dirty, save, reload]);

  const dispatch = useCallback((action: Action) => {
    const at = playheadRef.current;
    const current = history?.present ?? null;
    switch (action) {
      case "play-toggle": setPlayingOption(null); player.current?.toggle(); return;
      case "shuttle-back": setPlayingOption(null); player.current?.shuttle(-1); return;
      case "pause": player.current?.pause(); return;
      case "shuttle-forward": setPlayingOption(null); player.current?.shuttle(1); return;
      case "frame-back": player.current?.step(-1); return;
      case "frame-forward": player.current?.step(1); return;
      case "frames-back": player.current?.step(-10); return;
      case "frames-forward": player.current?.step(10); return;
      case "home": seekTo(0); return;
      case "end": seekTo(duration); return;
      case "previous-boundary": {
        if (!current) return;
        const before = boundaries(current).filter((point) => point < at - 0.02);
        if (before.length) seekTo(before[before.length - 1]);
        return;
      }
      case "next-boundary": {
        if (!current) return;
        const after = boundaries(current).find((point) => point > at + 0.02);
        if (after !== undefined) seekTo(after);
        return;
      }
      case "trim-in":
        if (current && rules) apply("inizio qui", trimToPlayhead(current, words, at, "start", rules));
        return;
      case "trim-out":
        if (current && rules) apply("fine qui", trimToPlayhead(current, words, at, "end", rules));
        return;
      case "split": {
        if (!current || !rules) return;
        const index = rangeAt(current, at);
        if (index === null) return;
        apply("dividi", splitRange(current, words, index, lastWordUpTo(words, at), rules));
        return;
      }
      case "remove": {
        if (!current) return;
        const index = selection?.kind === "range" ? selection.index : rangeAt(current, at);
        if (index !== null) apply("togli", removeRange(current, index));
        return;
      }
      case "restore": {
        if (!current || !rules) return;
        const gap = selection?.kind === "gap" ? selection.index : gapAt(current, at);
        if (gap !== null) apply("rimetti", restoreGap(current, words, gap, rules));
        return;
      }
      case "undo": setHistory((h) => (h ? undo(h) : h)); return;
      case "redo": setHistory((h) => (h ? redo(h) : h)); return;
      case "zoom-in": timeline.current?.zoomBy(1.6); return;
      case "zoom-out": timeline.current?.zoomBy(1 / 1.6); return;
      case "zoom-fit": timeline.current?.fit(); return;
      case "save": if (dirty) void save(); return;
      case "apply": void applyAndRebuild(); return;
      case "help": setHelp((value) => !value); return;
      case "escape": setHelp(false); setSelection(null); setPlayingOption(null); player.current?.stop(); return;
    }
  }, [history, rules, words, selection, duration, dirty, apply, seekTo, save, applyAndRebuild]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!plan) return;
      const action = actionFor(event);
      if (!action) return;
      event.preventDefault();
      dispatch(action);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [plan, dispatch]);

  // A dirty edit must not vanish with the tab.
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  // ------------------------------------------------------------------ view

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

  const source = plan?.source ?? "assets/raw.mov";
  // The proxy when it exists, the original only as a fallback.
  const mediaUrl = cut.media?.proxy ?? cut.analysis?.proxyUrl ?? `/media/${project.id}/${source}`;
  const manual = !!(plan && plan.manual && "kept" in plan.manual);

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

  const takePlan = (result: { plan: CutPlan; etag?: string }) =>
    setCut((current) =>
      current ? { ...current, state: result.plan.status, plan: result.plan, etag: result.etag ?? current.etag } : current,
    );

  const answer = (question: Question, option: string) =>
    run(question.id, async () => takePlan(await api.answer(project.id, question.id, option)));

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
          <a
            className="ghost link"
            href={`/api/projects/${project.id}/export/fcpxml`}
            download
            title="Il montaggio come timeline per DaVinci Resolve, Premiere Pro e Final Cut Pro (FCPXML); i sottotitoli a parte, in SRT"
          >
            Esporta per l'editor
          </a>
          <a className="ghost link small" href={`/api/projects/${project.id}/export/srt`} download title="Sottotitoli SRT">
            SRT
          </a>
          <button className="ghost" onClick={() => setCreating(true)}>Nuovo montaggio</button>
          {plan && <Status plan={plan} manual={manual} />}
          <button
            className="ghost"
            disabled={busy !== null || manual}
            title={manual ? "Il montaggio è stato corretto a mano: rigenerarlo lo perderebbe. «Riparti da zero» lo butta via e ricalcola." : "Ricalcola tenendo le scelte fatte a mano"}
            onClick={() => run("propose", async () => takePlan(await api.propose(project.id)))}
          >
            Rigenera proposta
          </button>
          <button
            className="ghost"
            disabled={busy !== null || (handAnswers(plan) === 0 && !manual)}
            title={
              manual
                ? "Butta via le correzioni a mano e riparte dal girato"
                : handAnswers(plan) > 0
                  ? `Butta via ${handAnswers(plan)} scelte fatte a mano e riparte dal girato`
                  : "Non c'è nessuna scelta fatta a mano da buttare"
            }
            onClick={() => {
              if (!window.confirm("Ripartire dal girato butta via ogni correzione fatta a mano. Procedo?")) return;
              void run("propose", async () => {
                takePlan(await api.propose(project.id, false));
                setNotice("Ripartito dal girato: tutte le scelte sono di nuovo del motore.");
              });
            }}
          >
            Riparti da zero
          </button>
          <button
            className="primary"
            disabled={busy !== null || !plan || (!manual && plan.stats.openQuestions > 0) || !!working}
            title={
              working
                ? "Il worker sta ancora lavorando su questo progetto"
                : plan && !manual && plan.stats.openQuestions > 0
                  ? "Restano decisioni aperte"
                  : "Scrive la timeline del progetto e rigenera video, sottotitoli e render (⌘↵)"
            }
            onClick={() => void applyAndRebuild()}
          >
            {busy === "apply" ? "Applico…" : "Applica e rigenera"}
          </button>
        </div>
      </header>

      {notice && <div className="notice">{notice}</div>}
      {conflict && (
        <div className="notice conflict">
          Qualcun altro ha modificato il montaggio nel frattempo.{" "}
          <button
            className="ghost small"
            onClick={() => {
              setCut((current) => (current ? { ...current, plan: conflict.current, etag: conflict.etag } : current));
              setHistory(null);
              setConflict(null);
              setSaveState("salvato");
            }}
          >
            Prendi la loro versione
          </button>{" "}
          <button
            className="ghost small"
            onClick={() => {
              setCut((current) => (current ? { ...current, etag: conflict.etag } : current));
              setConflict(null);
              setSaveState("non salvato");
              window.setTimeout(() => void save(), 0);
            }}
          >
            Sovrascrivi con la mia
          </button>
        </div>
      )}
      {sheet}
      {help && <ShortcutsHelp onClose={() => setHelp(false)} />}

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
          onPropose={() => run("propose", async () => takePlan(await api.propose(project.id)))}
        />
      ) : (
        <main className="workspace">
          <section className="stage">
            <Player
              ref={player}
              src={mediaUrl}
              fps={fps}
              getSegments={() => segmentsRef.current}
              onTime={setPlayhead}
              onPlayingChange={(value) => {
                setPlaying(value);
                if (!value) setPlayingOption(null);
              }}
              onError={(message) => setNotice(message)}
            />
            <div className="transport">
              <div className="transport-keys">
                <button title="Indietro (J)" onClick={() => dispatch("shuttle-back")}>◀◀</button>
                <button title="Un fotogramma indietro (←)" onClick={() => dispatch("frame-back")}>◀</button>
                <button
                  className="play"
                  disabled={!mediaUrl}
                  title="Riproduci / pausa il montaggio (spazio)"
                  onClick={() => dispatch("play-toggle")}
                >
                  {playing && !playingOption ? "◼" : "▶"}
                </button>
                <button title="Un fotogramma avanti (→)" onClick={() => dispatch("frame-forward")}>▶</button>
                <button title="Avanti (L)" onClick={() => dispatch("shuttle-forward")}>▶▶</button>
              </div>
              <span className="timecode" title="minuti:secondi:fotogrammi nel girato">
                {formatTimecode(playhead, fps)}
              </span>
              <span className="hint">
                salta le parti tolte · {state ? formatTime(segmentsOf(state).reduce((sum, s) => sum + s.end - s.start, 0)) : ""} montati
              </span>
              {!cut.media?.ready && !cut.media?.job && (
                <button
                  className="ghost small"
                  title="Prepara il proxy, la forma d'onda e le miniature"
                  onClick={() => run("media", async () => { await api.requestMedia(project.id); setNotice("Anteprima in preparazione nel worker."); })}
                >
                  Prepara anteprima
                </button>
              )}
            </div>
          </section>

          <Questions
            questions={plan.questions}
            selected={selected}
            busy={busy}
            playingOption={playingOption}
            onSelect={(question) => {
              setSelected(question);
              seekTo(question.at);
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

          {transcript && state && rules && (
            <div className="timeline">
              <div className="timeline-bar">
                <span className="eyebrow">TIMELINE DEL GIRATO</span>
                <div className="zoom">
                  <button onClick={() => dispatch("zoom-out")} aria-label="Riduci zoom">−</button>
                  <button onClick={() => dispatch("zoom-fit")} aria-label="Tutto il girato">⊡</button>
                  <button onClick={() => dispatch("zoom-in")} aria-label="Aumenta zoom">+</button>
                </div>
                <div className="undo">
                  <button
                    disabled={!history?.past.length}
                    title={history?.past.length ? `Annulla: ${history.label} (⌘Z)` : "Niente da annullare"}
                    onClick={() => dispatch("undo")}
                  >
                    ↶ annulla
                  </button>
                  <button
                    disabled={!history?.future.length}
                    title={history?.future.length ? `Ripeti: ${history.future[0].label} (⇧⌘Z)` : "Niente da ripetere"}
                    onClick={() => dispatch("redo")}
                  >
                    ripeti ↷
                  </button>
                </div>
                <button className="ghost small" onClick={() => setHelp(true)} title="Scorciatoie (?)">?</button>
                <div className={`save ${saveState === "non salvato" ? "dirty" : saveState === "conflitto" ? "conflict" : ""}`} style={{ marginLeft: "auto" }}>
                  <span>{saveState}</span>
                  <button disabled={!dirty || saveState === "salvataggio…"} onClick={() => void save()} title="Salva le correzioni (⌘S)">
                    Salva
                  </button>
                </div>
              </div>
              <EditorTimeline
                ref={timeline}
                state={state}
                preview={preview}
                words={words}
                silences={transcript.silences}
                duration={duration}
                fps={fps}
                rules={rules}
                removed={plan.removed}
                engineKept={engineKeptOf(plan)}
                questions={plan.questions}
                selectedQuestion={selected}
                media={cut.media}
                playhead={playhead}
                playing={playing}
                selection={selection}
                onSeek={seekTo}
                onSelect={setSelection}
                onSelectQuestion={(question) => { setSelected(question); seekTo(question.at); }}
                onPreview={setPreview}
                onCommit={apply}
              />
            </div>
          )}
        </main>
      )}
    </div>
  );
}

/** Which words the engine kept before anyone touched the cut: remembered by
 *  the manual layer once there is one, read off the segments until then. */
function engineKeptOf(plan: CutPlan): [number, number][] {
  if (plan.manual && "engineKept" in plan.manual) return plan.manual.engineKept;
  return plan.segments
    .filter((segment) => segment.firstWord !== null && segment.lastWord !== null)
    .map((segment) => [segment.firstWord as number, segment.lastWord as number]);
}

/** How many choices a person made by hand. Those are the sticky ones: a
 *  rebuild never overturns them, which is why there has to be a way out. */
function handAnswers(plan: CutPlan | null): number {
  if (!plan) return 0;
  return plan.questions.filter(
    (question) => question.answer !== null && question.answeredBy !== ENGINE,
  ).length;
}

function Status({ plan, manual }: { plan: CutPlan; manual: boolean }) {
  const removed = Math.round(plan.stats.removedShare * 100);
  const tone = plan.status === "ready" ? "ok" : plan.status === "applied" ? "done" : "draft";
  // "Pronto" on its own would hide the judgement calls the engine made to get
  // there. Say how many, so the number is an invitation to look at them.
  const byEngine = plan.questions.filter((question) => question.answeredBy === ENGINE).length;
  const state = manual
    ? "corretto a mano"
    : plan.stats.openQuestions > 0
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
    case "rebuild": return "rigenerazione (video, sottotitoli, render)";
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
