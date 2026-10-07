import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type ConflictPayload, type JobSummary, type Me, type ProjectSummary } from "./api";
import { AcceptInvite } from "./components/AcceptInvite";
import { Inspector } from "./components/Inspector";
import { Intake } from "./components/Intake";
import { NewProject } from "./components/NewProject";
import { People } from "./components/People";
import { Player, type Clock, type PlayerHandle } from "./components/Player";
import { ENGINE, Questions, reviewQueue } from "./components/Questions";
import { ShortcutsHelp } from "./components/ShortcutsHelp";
import { Icon } from "./components/icons";
import { Timeline, type Selection, type TimelineHandle, type TrimInfo } from "./components/Timeline";
import { formatTime, formatTimecode } from "./components/timeline/time";
import { TrimView } from "./components/TrimView";
import { Transport } from "./components/Transport";
import { actionFor, type Action } from "./edit/keys";
import {
  commit, fromPlan, historyOf, redo, removeRange, restoreGap, rulesOf, sameCut, splitRange,
  toRequest, trimToPlayhead, undo, type EditState, type History,
} from "./edit/model";
import { build, clipAt, joins, sourceAt, type CutSource, type Sequence } from "./edit/sequence";
import type { CutPlan, CutState, Option, Question, Transcript } from "./types";

type SaveState = "salvato" | "non salvato" | "salvataggio…" | "conflitto";
/** An invitation link lands here as `/app/?invito=<token>`. */
const INVITE = new URLSearchParams(window.location.search).get("invito");

/** The value the project menu uses for its last entry, which is not a project. */
const NEW = "__nuovo__";
const EMPTY: Sequence = { clips: [], cuts: [], duration: 0 };

export default function App() {
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [creating, setCreating] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [people, setPeople] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [working, setWorking] = useState<JobSummary | null>(null);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [cut, setCut] = useState<CutState | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [selectedQuestion, setSelectedQuestion] = useState<Question | null>(null);
  const [playing, setPlaying] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playingOption, setPlayingOption] = useState<string | null>(null);
  const [help, setHelp] = useState(false);
  const [tab, setTab] = useState<"clip" | "decisioni">("clip");
  const [snapping, setSnapping] = useState(true);
  const [zoom, setZoom] = useState({ px: 0, fit: 1 });
  const [trimming, setTrimming] = useState<TrimInfo["side"] | null>(null);
  const [trackHeight, setTrackHeight] = useState(() => remembered("skyground.trackHeight", 236));
  const [exportOpen, setExportOpen] = useState(false);

  const [history, setHistory] = useState<History | null>(null);
  const [baseline, setBaseline] = useState<EditState | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("salvato");
  const [conflict, setConflict] = useState<ConflictPayload | null>(null);

  const player = useRef<PlayerHandle>(null);
  const timeline = useRef<TimelineHandle>(null);
  const clock = useRef(0) as Clock;
  /** The film's length while a drag is in flight, for the readouts that show
   *  it without waiting for the edit to be committed. */
  const film = useRef(0) as Clock;
  const sequenceRef = useRef<Sequence>(EMPTY);
  const previewRef = useRef<Sequence | null>(null);
  const trimRef = useRef<TrimInfo | null>(null);
  /** One refresh of the signed media links at a time, and not in a loop. */
  const refreshing = useRef(false);

  useEffect(() => {
    // Somebody opening an invitation has no session yet: no projects to load,
    // and the redirect to the sign-in page would send them away from it.
    if (INVITE) return;
    void (async () => {
      try {
        const list = await api.projects();
        setProjects(list);
        setLoaded(true);
        if (list.length) setProject(list[0]);
        api.me().then((answer) => setMe(answer.user)).catch(() => undefined);
      } catch (cause) {
        // Without a session there is nothing to show here, and the sign-in
        // form lives on the panel at the root. Send the visitor there.
        if (cause instanceof ApiError && cause.status === 401) { window.location.replace("/"); return; }
        setError(String(cause));
      }
    })();
  }, []);

  const reload = useCallback(async (slug: string) => {
    const state = await api.cut(slug);
    setCut(state);
    if (state.analysis) setTranscript(await api.transcript(slug));
    return state;
  }, []);

  useEffect(() => {
    if (!project) return;
    setHistory(null); setBaseline(null); setSelection(null); setConflict(null);
    clock.current = 0;
    void reload(project.id).catch((cause) => setError(String(cause)));
  }, [project, reload]);

  // The plan from the server is the baseline of the edit: when it changes the
  // timeline starts from it again, unless somebody is mid-edit on the same one.
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

  const plan = cut?.plan ?? null;
  const words = transcript?.words ?? [];
  const sourceDuration = transcript?.duration ?? plan?.sourceDuration ?? 0;
  const fps = cut?.media?.fps ?? project?.canvas.fps ?? 30;
  const rules = useMemo(() => (plan ? rulesOf(plan, sourceDuration, fps) : null), [plan, sourceDuration, fps]);
  const manual = !!(plan && plan.manual && "kept" in plan.manual);

  const source: CutSource = useMemo(() => ({
    removed: plan?.removed ?? [],
    engineKept: engineKeptOf(plan),
    sourceDuration,
  }), [plan, sourceDuration]);

  const state = history?.present ?? null;
  const sequence = useMemo(
    () => (state ? build(state, words, source) : EMPTY),
    [state, words, source],
  );
  sequenceRef.current = sequence;
  if (!previewRef.current) film.current = sequence.duration;
  const dirty = !!(state && baseline && !sameCut(state, baseline));

  useEffect(() => {
    setSaveState((current) => (current === "conflitto" ? current : dirty ? "non salvato" : "salvato"));
  }, [dirty]);

  // The worker does the slow parts; the documents only change when it is done.
  useEffect(() => {
    if (!project) return;
    setJobs([]);
    let previous: JobSummary | null = null;
    let stopped = false;
    let timer = 0;
    const tick = async () => {
      try {
        const jobs = await api.jobs(project.id);
        const live = jobs.find((job) => job.status === "queued" || job.status === "running") ?? null;
        if (!stopped) { setWorking(live); setJobs(jobs); }
        if (previous && !live) {
          const ended = jobs.find((job) => job.id === previous?.id);
          const render = (ended?.result as { render?: { skipped?: boolean; reason?: string } } | null)?.render;
          if (ended?.status === "failed") setNotice(`${describe(ended.kind)}: fallito — ${ended.error ?? ""}`);
          else if (ended?.status === "succeeded" && render?.skipped) setNotice(`${describe(ended.kind)}: documenti aggiornati. ${render.reason ?? ""}`);
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

  useEffect(() => {
    if (!cut?.plan) return setSelectedQuestion(null);
    const queue = reviewQueue(cut.plan.questions);
    setSelectedQuestion((current) => queue.find((q) => q.id === current?.id) ?? queue[0] ?? null);
  }, [cut]);

  // --------------------------------------------------------------- commands

  const change = useCallback((label: string, next: EditState | null) => {
    if (!next) return;
    setHistory((current) => (current ? commit(current, label, next) : current));
  }, []);

  const seek = useCallback((output: number) => {
    setPlayingOption(null);
    player.current?.seek(output);
    timeline.current?.reveal(output);
    timeline.current?.redraw();
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
        `Applicato (revisione ${result.revision}): ${result.clips} clip, ${result.duration.toFixed(1)}s` +
          (result.job ? " · il worker rigenera video, sottotitoli e render" : ""),
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

  const selectedClip = selection?.kind === "clip" ? sequence.clips[selection.index] ?? null : null;
  const selectedCut = selection?.kind === "cut"
    ? sequence.cuts.find((item) => item.index === selection.index) ?? null
    : null;

  const splitHere = useCallback(() => {
    if (!state || !rules) return;
    const clip = clipAt(sequence, clock.current);
    if (!clip) return;
    const at = sourceAt(sequence, clock.current);
    let after = clip.first;
    for (let index = clip.first; index < clip.last; index += 1) if (words[index].end <= at) after = index;
    change("dividi", splitRange(state, words, clip.index, after, rules));
  }, [state, rules, sequence, words, change]);

  const removeHere = useCallback(() => {
    if (!state) return;
    const index = selection?.kind === "clip" ? selection.index : clipAt(sequence, clock.current)?.index ?? null;
    if (index === null) return;
    change("togli", removeRange(state, index));
    setSelection(null);
  }, [state, selection, sequence, change]);

  const restoreHere = useCallback(() => {
    if (!state || !rules) return;
    const gap = selection?.kind === "cut"
      ? selection.index
      : sequence.cuts.find((item) => Math.abs(item.at - clock.current) < 0.35)?.index ?? null;
    if (gap === null) return;
    change("rimetti", restoreGap(state, words, gap, rules));
    setSelection(null);
  }, [state, rules, selection, sequence, words, change]);

  const dispatch = useCallback((action: Action) => {
    const current = history?.present ?? null;
    switch (action) {
      case "play-toggle": setPlayingOption(null); player.current?.toggle(); return;
      case "shuttle-back": setPlayingOption(null); player.current?.shuttle(-1); return;
      case "pause": player.current?.pause(); return;
      case "shuttle-forward": setPlayingOption(null); player.current?.shuttle(1); return;
      case "frame-back": player.current?.step(-1); timeline.current?.redraw(); return;
      case "frame-forward": player.current?.step(1); timeline.current?.redraw(); return;
      case "frames-back": player.current?.step(-10); timeline.current?.redraw(); return;
      case "frames-forward": player.current?.step(10); timeline.current?.redraw(); return;
      case "home": seek(0); return;
      case "end": seek(sequence.duration); return;
      case "previous-boundary": {
        const before = joins(sequence).filter((point) => point < clock.current - 0.02);
        seek(before.length ? before[before.length - 1] : 0);
        return;
      }
      case "next-boundary": {
        const after = joins(sequence).find((point) => point > clock.current + 0.02);
        if (after !== undefined) seek(after);
        return;
      }
      case "select-previous":
      case "select-next": {
        if (!sequence.clips.length) return;
        const at = selection?.kind === "clip" ? selection.index : clipAt(sequence, clock.current)?.index ?? 0;
        const next = Math.max(0, Math.min(sequence.clips.length - 1, at + (action === "select-next" ? 1 : -1)));
        setSelection({ kind: "clip", index: next });
        seek(sequence.clips[next].outputStart);
        return;
      }
      case "trim-in":
        if (current && rules) change("inizio qui", trimToPlayhead(current, words, sourceAt(sequence, clock.current), "start", rules));
        return;
      case "trim-out":
        if (current && rules) change("fine qui", trimToPlayhead(current, words, sourceAt(sequence, clock.current), "end", rules));
        return;
      case "split": splitHere(); return;
      case "remove": removeHere(); return;
      case "restore": restoreHere(); return;
      case "undo": setHistory((h) => (h ? undo(h) : h)); setSelection(null); return;
      case "redo": setHistory((h) => (h ? redo(h) : h)); setSelection(null); return;
      case "zoom-in": timeline.current?.zoomBy(1.5); return;
      case "zoom-out": timeline.current?.zoomBy(1 / 1.5); return;
      case "zoom-fit": timeline.current?.fit(); return;
      case "toggle-snap": setSnapping((value) => !value); return;
      case "save": if (dirty) void save(); return;
      case "apply": void applyAndRebuild(); return;
      case "help": setHelp((value) => !value); return;
      case "escape":
        setHelp(false); setExportOpen(false); setSelection(null); setPlayingOption(null); player.current?.stop();
        return;
    }
  }, [history, rules, words, sequence, selection, dirty, change, seek, save, applyAndRebuild, splitHere, removeHere, restoreHere]);

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

  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  // ------------------------------------------------------------------ view

  /** One door into a new film: the button and the project menu both open
   *  this, and it says what is being left before it takes a name. */
  const sheet = creating && (
    <NewProject
      leaving={project ? { name: project.name, dirty, onSave: save } : null}
      onClose={() => setCreating(false)}
      onCreated={(made) => {
        setProjects((current) => [made, ...current]);
        setCut(null); setTranscript(null); setHistory(null); setBaseline(null);
        setSelection(null); setConflict(null); setSaveState("salvato"); setTab("clip");
        clock.current = 0;
        setProject(made);
      }}
    />
  );

  if (INVITE) return <AcceptInvite token={INVITE} onDone={() => window.location.replace("/app/")} />;
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

  // The camera original is usually HEVC in a .mov, which no browser decodes:
  // offering it to the player only produced «il video non si carica» while the
  // preview was still being made. Without a playable copy, say so instead.
  const camera = plan?.source ?? "assets/raw.mov";
  const playableCamera = /\.(mp4|m4v|webm)$/i.test(camera) ? `/media/${project.id}/${camera}` : null;
  const src = cut.media?.proxy ?? cut.analysis?.proxyUrl ?? playableCamera;
  const open = plan ? plan.questions.filter((q) => !q.resolved).length : 0;
  const engineDecided = plan ? plan.questions.filter((q) => q.answeredBy === ENGINE).length : 0;

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label); setNotice(null);
    try { await action(); }
    catch (cause) { setNotice(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  };
  const takePlan = (result: { plan: CutPlan; etag?: string }) =>
    setCut((current) => (current ? { ...current, state: result.plan.status, plan: result.plan, etag: result.etag ?? current.etag } : current));

  return (
    <div className="editor">
      <header className="bar">
        <div className="bar-left">
          <span className="mark" aria-hidden>S</span>
          <select
            className="picker"
            value={project.id}
            aria-label="Progetto"
            onChange={(event) => {
              if (event.target.value === NEW) { setCreating(true); return; }
              const next = projects.find((item) => item.id === event.target.value);
              if (next && next.id !== project.id) { setCut(null); setTranscript(null); setHistory(null); setBaseline(null); setSelection(null); setProject(next); }
            }}
          >
            {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            <option value={NEW}>＋ Nuovo video…</option>
          </select>
          {plan && (
            <Meter film={film} source={sourceDuration} />
          )}
        </div>

        <div className="bar-right">
          {working ? (
            <span className="status live"><i />{describe(working.kind)}…</span>
          ) : plan && (
            <span className={`status ${saveState.replace("…", "").replace(" ", "-")}`}><i />{saveState}</span>
          )}
          <button className="btn ghost" onClick={() => setCreating(true)}>Nuovo</button>
          {me && me.email !== "local@skyground.local" && (
            <button className="avatar" onClick={() => setPeople(true)} title={me.isAdmin ? "Persone e la tua password" : "La tua password"} aria-label="Persone">
              {(me.name || me.email).slice(0, 1).toUpperCase()}
            </button>
          )}
          <div className="dropdown">
            <button className="btn ghost" aria-expanded={exportOpen} onClick={() => setExportOpen((value) => !value)}>
              Esporta <Icon name="chevron" size={14} />
            </button>
            {exportOpen && (
              <>
                <div className="dropdown-away" onClick={() => setExportOpen(false)} />
                <div className="dropdown-menu" role="menu" onClick={() => setExportOpen(false)}>
                  <a role="menuitem" href={`/api/projects/${project.id}/export/fcpxml`} download>
                    <Icon name="download" /> <span><b>Timeline FCPXML</b><small>DaVinci Resolve, Premiere, Final Cut</small></span>
                  </a>
                  <a role="menuitem" href={`/api/projects/${project.id}/export/srt`} download>
                    <Icon name="download" /> <span><b>Sottotitoli SRT</b><small>Le parole del montaggio, a tempo</small></span>
                  </a>
                </div>
              </>
            )}
          </div>
          <button
            className="btn primary"
            disabled={busy !== null || !plan || (!manual && open > 0) || !!working}
            title={working ? "Il worker sta ancora lavorando" : open > 0 && !manual ? `Restano ${open} decisioni aperte` : "Scrive la timeline e rigenera tutto (⌘↵)"}
            onClick={() => void applyAndRebuild()}
          >
            {busy === "apply" ? "Applico…" : "Applica e rigenera"}
          </button>
        </div>
      </header>

      {notice && <div className="notice" onClick={() => setNotice(null)} role="status">{notice}</div>}
      {conflict && (
        <div className="notice warn">
          Qualcun altro ha modificato il montaggio.
          <button className="btn tiny" onClick={() => {
            setCut((current) => (current ? { ...current, plan: conflict.current, etag: conflict.etag } : current));
            setHistory(null); setConflict(null); setSaveState("salvato");
          }}>Prendi la loro</button>
          <button className="btn tiny ghost" onClick={() => {
            setCut((current) => (current ? { ...current, etag: conflict.etag } : current));
            setConflict(null); setSaveState("non salvato");
            window.setTimeout(() => void save(), 0);
          }}>Sovrascrivi</button>
        </div>
      )}
      {sheet}
      {help && <ShortcutsHelp onClose={() => setHelp(false)} />}
      {people && me && <People me={me} onClose={() => setPeople(false)} />}

      {project.hasSource === false || (!plan && (working || jobs.some((job) => job.kind === "analyze" || job.kind === "full"))) ? (
        <Intake
          project={project}
          jobs={working && !jobs.some((job) => job.id === working.id) ? [working, ...jobs] : jobs}
          onUploaded={(updated, job) => {
            setProjects((current) => current.map((item) => (item.id === updated.id ? updated : item)));
            setWorking(job);
            setJobs([job]);
            setProject(updated);
          }}
          onRetry={() => run("analyze", async () => { const job = await api.analyze(project.id); setWorking(job as JobSummary); })}
        />
      ) : !plan || !state || !rules ? (
        <Missing
          state={cut.state}
          busy={busy !== null}
          onAnalyze={() => run("analyze", () => api.analyze(project.id))}
          onPropose={() => run("propose", async () => takePlan(await api.propose(project.id)))}
        />
      ) : (
        <>
          <main className="stage">
            <section className="viewer">
              <Player
                ref={player}
                src={src}
                fps={fps}
                clock={clock}
                getSequence={() => previewRef.current ?? sequenceRef.current}
                onPlayingChange={(value) => { setPlaying(value); if (!value) setPlayingOption(null); }}
                onError={(message) => {
                  // A signed media link lasts four hours; a page left open all
                  // afternoon then plays nothing and blames the video. Ask the
                  // server for a fresh one before saying anything to anybody.
                  if (refreshing.current) { setNotice(message); return; }
                  refreshing.current = true;
                  void api.cut(project.id)
                    .then((fresh) => { setCut(fresh); })
                    .catch(() => setNotice(message))
                    .finally(() => { window.setTimeout(() => { refreshing.current = false; }, 4000); });
                }}
              />
              {src && (
                <div className={trimming ? "trim-layer on" : "trim-layer"} aria-hidden={!trimming}>
                  <TrimView src={src} fps={fps} trim={trimRef} side={trimming} />
                </div>
              )}
              <Transport
                clock={clock}
                duration={film}
                fps={fps}
                playing={playing}
                disabled={!src}
                onAction={(what) => {
                  if (what === "toggle") dispatch("play-toggle");
                  else if (what === "frame-back") dispatch("frame-back");
                  else if (what === "frame-forward") dispatch("frame-forward");
                  else if (what === "prev-cut") dispatch("previous-boundary");
                  else dispatch("next-boundary");
                }}
              />
            </section>

            <aside className="panel">
              <div className="tabs" role="tablist">
                <button role="tab" aria-selected={tab === "clip"} className={tab === "clip" ? "on" : ""} onClick={() => setTab("clip")}>Selezione</button>
                <button role="tab" aria-selected={tab === "decisioni"} className={tab === "decisioni" ? "on" : ""} onClick={() => setTab("decisioni")}>
                  Decisioni {plan.questions.length > 0 && <i className={open > 0 ? "count open" : "count"}>{open > 0 ? open : engineDecided}</i>}
                </button>
              </div>
              <div className="panel-body">
                {tab === "clip" ? (
                  <Inspector
                    clip={selectedClip}
                    cut={selectedCut}
                    words={words}
                    fps={fps}
                    onSplit={splitHere}
                    onRemove={removeHere}
                    onRestore={restoreHere}
                    onListen={() => {
                      if (!selectedCut) return;
                      const from = sourceAt(sequence, selectedCut.at);
                      player.current?.playRange(Math.max(0, from - selectedCut.removed - 0.4), from + 0.6);
                    }}
                  />
                ) : (
                  <Questions
                    questions={plan.questions}
                    selected={selectedQuestion}
                    busy={busy}
                    playingOption={playingOption}
                    onSelect={(question) => { setSelectedQuestion(question); seek(outputOf(sequence, question.at)); }}
                    onAnswer={(question, option) =>
                      run(question.id, async () => takePlan(await api.answer(project.id, question.id, option)))
                    }
                    onListen={(question, option: Option) => {
                      if (option.start === undefined || option.end === undefined) return;
                      if (playingOption === option.id) { setPlayingOption(null); player.current?.stop(); return; }
                      setSelectedQuestion(question);
                      setPlayingOption(option.id);
                      player.current?.playRange(option.start, option.end);
                    }}
                  />
                )}
              </div>
            </aside>
          </main>

          <div
            className="divider"
            role="separator"
            aria-orientation="horizontal"
            aria-label="Altezza della timeline"
            onPointerDown={(event) => {
              const from = event.clientY;
              const start = trackHeight;
              const target = event.currentTarget;
              target.setPointerCapture(event.pointerId);
              const move = (next: PointerEvent) => {
                const wanted = start - (next.clientY - from);
                setTrackHeight(Math.round(Math.max(170, Math.min(window.innerHeight - 320, wanted))));
              };
              const up = () => {
                target.removeEventListener("pointermove", move);
                target.removeEventListener("pointerup", up);
                setTrackHeight((value) => { remember("skyground.trackHeight", value); return value; });
              };
              target.addEventListener("pointermove", move);
              target.addEventListener("pointerup", up);
            }}
            onDoubleClick={() => { setTrackHeight(236); remember("skyground.trackHeight", 236); }}
          />

          <section className="track-panel">
            <div className="tools">
              <Timecode clock={clock} fps={fps} />
              <div className="group">
                <button className="tool icon" disabled={!history?.past.length} data-tip={history?.past.length ? `Annulla: ${history.label}  ⌘Z` : "Niente da annullare"} onClick={() => dispatch("undo")} aria-label="Annulla"><Icon name="undo" /></button>
                <button className="tool icon" disabled={!history?.future.length} data-tip={history?.future.length ? `Ripeti: ${history.future[0].label}  ⇧⌘Z` : "Niente da ripetere"} onClick={() => dispatch("redo")} aria-label="Ripeti"><Icon name="redo" /></button>
              </div>
              <div className="group">
                <button className="tool icon" data-tip="Dividi al playhead  S" onClick={splitHere} disabled={!clipAt(sequence, clock.current)} aria-label="Dividi"><Icon name="split" /></button>
                <button className="tool icon" data-tip="Togli la clip  ⌫" onClick={removeHere} disabled={selection?.kind !== "clip" && !clipAt(sequence, clock.current)} aria-label="Togli"><Icon name="trash" /></button>
                <button className="tool icon" data-tip="Rimetti la parte tolta  R" onClick={restoreHere} disabled={selection?.kind !== "cut"} aria-label="Rimetti"><Icon name="restore" /></button>
              </div>
              <div className="group">
                <button className={snapping ? "tool icon on" : "tool icon"} data-tip={snapping ? "Aggancio acceso  N · tieni ⌘ per trascinare libero" : "Aggancio spento  N"} onClick={() => setSnapping((value) => !value)} aria-pressed={snapping} aria-label="Aggancio"><Icon name="magnet" /></button>
              </div>
              <div className="spacer" />
              {dirty && (
                <button className="btn tiny" onClick={() => void save()} disabled={saveState === "salvataggio…"} title="Salva le correzioni (⌘S)">Salva</button>
              )}
              <button className="tool icon" data-tip="Scorciatoie  ?" onClick={() => setHelp(true)} aria-label="Scorciatoie"><Icon name="help" /></button>
              <div className="group zoom">
                <button className="tool icon" data-tip="Allontana  −" onClick={() => dispatch("zoom-out")} aria-label="Allontana"><Icon name="minus" /></button>
                <input
                  type="range" min={0} max={1} step={0.001}
                  value={zoomValue(zoom)}
                  aria-label="Zoom"
                  onChange={(event) => {
                    const wanted = zoomFrom(Number(event.target.value), zoom.fit);
                    timeline.current?.zoomBy(wanted / (zoom.px || zoom.fit));
                  }}
                />
                <button className="tool icon" data-tip="Avvicina  +" onClick={() => dispatch("zoom-in")} aria-label="Avvicina"><Icon name="plus" /></button>
                <button className="tool icon" data-tip="Tutto il montaggio  0" onClick={() => dispatch("zoom-fit")} aria-label="Adatta"><Icon name="fit" /></button>
              </div>
            </div>

            <Timeline
              ref={timeline}
              state={state}
              words={words}
              silences={transcript?.silences ?? []}
              rules={rules}
              source={source}
              media={cut.media}
              fps={fps}
              questions={plan.questions}
              selection={selection}
              playhead={clock}
              playing={playing}
              snapping={snapping}
              height={trackHeight}
              onTrim={(info) => {
                trimRef.current = info;
                const side = info?.side ?? null;
                setTrimming((current) => (current === side ? current : side));
              }}
              onScrub={(output) => { setPlayingOption(null); player.current?.seek(output); }}
              onSelect={(next) => { setSelection(next); if (next) setTab("clip"); }}
              onCommit={change}
              onPreview={(preview) => {
                previewRef.current = preview?.sequence ?? null;
                film.current = preview?.sequence.duration ?? sequence.duration;
              }}
              onZoom={(px, fit) => setZoom({ px, fit })}
            />
          </section>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ pieces

/** The film's length, beside the footage's. Written from an animation frame
 *  so a trim shows its effect on the whole film while the hand is moving. */
function Meter({ film, source }: { film: Clock; source: number }) {
  const readout = useRef<HTMLElement>(null);
  useEffect(() => {
    let frame = 0;
    let shown = -1;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const now = Math.round(film.current * 10);
      if (now === shown || !readout.current) return;
      shown = now;
      readout.current.textContent = formatTime(film.current);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [film]);
  return (
    <span className="meter" title="Durata del montaggio, e del girato da cui viene">
      <b ref={readout}>{formatTime(film.current)}</b>
      <i>da {formatTime(source)}</i>
    </span>
  );
}

/** Where the film is, beside the tools, the way Premiere puts it over the
 *  timeline. Written from an animation frame like the transport's. */
function Timecode({ clock, fps }: { clock: Clock; fps: number }) {
  const readout = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    let frame = 0;
    let shown = -1;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const now = Math.round(clock.current * fps);
      if (now === shown || !readout.current) return;
      shown = now;
      readout.current.textContent = formatTimecode(clock.current, fps);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [clock, fps]);
  return <span className="tc" ref={readout}>{formatTimecode(clock.current, fps)}</span>;
}

/** A per-viewer preference; storage can be missing or refuse, and then the
 *  default is fine. */
function remembered(key: string, fallback: number): number {
  try {
    const value = Number(window.localStorage.getItem(key));
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch {
    return fallback;
  }
}
function remember(key: string, value: number) {
  try { window.localStorage.setItem(key, String(value)); } catch { /* not kept: fine */ }
}

/** Which words the engine kept before anyone touched the cut: remembered by
 *  the manual layer once there is one, read off the segments until then. */
function engineKeptOf(plan: CutPlan | null): [number, number][] {
  if (!plan) return [];
  if (plan.manual && "engineKept" in plan.manual) return plan.manual.engineKept;
  return plan.segments
    .filter((segment) => segment.firstWord !== null && segment.lastWord !== null)
    .map((segment) => [segment.firstWord as number, segment.lastWord as number]);
}

function outputOf(sequence: Sequence, source: number): number {
  for (const clip of sequence.clips) {
    if (source < clip.start) return clip.outputStart;
    if (source <= clip.end) return clip.outputStart + (source - clip.start);
  }
  return sequence.duration;
}

/** The zoom slider runs on a log scale: linear, the useful range is a sliver. */
function zoomValue(zoom: { px: number; fit: number }): number {
  const px = zoom.px || zoom.fit;
  return Math.max(0, Math.min(1, Math.log(px / zoom.fit) / Math.log(600 / zoom.fit)));
}
function zoomFrom(value: number, fit: number): number {
  return fit * Math.pow(600 / fit, value);
}

function describe(kind: string): string {
  switch (kind) {
    case "full": return "montaggio completo";
    case "rebuild": return "rigenerazione";
    case "analyze": return "trascrizione del girato";
    case "render": return "render del film";
    case "proxy": return "anteprima del girato";
    case "sync": return "sincronizzazione";
    case "validate": return "validazione";
    default: return kind;
  }
}

function Missing({ state, busy, onAnalyze, onPropose }: {
  state: string; busy: boolean; onAnalyze: () => void; onPropose: () => void;
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

function Empty({ title, detail, action }: {
  title: string; detail?: string; action?: { label: string; onClick: () => void; disabled?: boolean };
}) {
  return (
    <div className="blank">
      <h2>{title}</h2>
      {detail && <p>{detail}</p>}
      {action && <button className="btn primary" onClick={action.onClick} disabled={action.disabled}>{action.label}</button>}
    </div>
  );
}
