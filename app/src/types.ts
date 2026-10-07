/** The shapes the Python engine produces. Kept narrow on purpose: the app
 *  renders a proposal, it never recomputes one. */

export interface Word {
  t: number;
  end: number;
  s: string;
  p: number;
}

export interface Segment {
  start: number;
  end: number;
  label: string;
  firstWord: number | null;
  lastWord: number | null;
}

export type RemovalReason =
  | "silence"
  | "lead-in"
  | "lead-out"
  | "filler"
  | "retake"
  | "manual";

export interface Removed {
  start: number;
  end: number;
  reason: RemovalReason;
  confidence: number;
  detail: string;
}

export interface Option {
  id: string;
  label: string;
  detail: string;
  recommended: boolean;
  /** The piece of source this option is about, when it is about one. Present
   *  so the option can be *heard* rather than read. */
  start?: number;
  end?: number;
}

export type QuestionKind =
  | "take-choice"
  | "pause-intent"
  | "filler-inside"
  | "boundary"
  | "low-confidence";

export interface Question {
  id: string;
  kind: QuestionKind;
  at: number;
  prompt: string;
  context: string;
  options: Option[];
  answer: string | null;
  answeredBy: string | null;
  resolved: boolean;
}

export interface Utterance {
  index: number;
  firstWord: number;
  lastWord: number;
  start: number;
  end: number;
  text: string;
  takeGroup: number | null;
  kept: boolean;
  dropReason: string;
}

export interface Stats {
  sourceDuration: number;
  outputDuration: number;
  removedDuration: number;
  removedShare: number;
  segments: number;
  openQuestions: number;
  questions: number;
}

export type PlanStatus = "draft" | "ready" | "applied";

/** The cut as a person left it on the timeline: what the server realised the
 *  plan from, and what the engine had kept before anyone touched it. */
export interface ManualLayer {
  kept: { first: number; last: number; start?: number; end?: number }[];
  engineKept: [number, number][];
  editedAt: string;
  editedBy: string;
  basedOnTimelineEtag: string;
  appliedRevision?: number;
}

/** Every number the engine has an opinion about; the ones the editor needs. */
export interface Policy {
  lead_in: number;
  lead_out: number;
  min_segment: number;
  [key: string]: number | boolean;
}

export interface CutPlan {
  source: string;
  sourceDuration: number;
  status: PlanStatus;
  generatedAt: string;
  appliedAt: string;
  stats: Stats;
  segments: Segment[];
  removed: Removed[];
  questions: Question[];
  utterances: Utterance[];
  policy: Policy;
  manual: ManualLayer | Record<string, never>;
}

export interface AnalysisSummary {
  source: string;
  duration: number;
  words: number;
  provider: string;
  generatedAt: string;
  /** A browser playable copy, when the worker has produced one. The camera
   *  original is usually HEVC, which no browser decodes. */
  proxyUrl: string | null;
}

/** Where the proxy, the peaks and the thumbnail sheets are, signed for hours. */
export interface MediaInfo {
  ready: boolean;
  job?: { id: string; kind: string; status: string } | null;
  proxy?: string;
  codec?: string;
  fps?: number;
  gop?: number;
  duration?: number;
  peaks?: string;
  peaksRate?: number;
  thumbs?: { urls: string[]; count: number; columns: number; rows: number; perSheet: number;
    width: number; height: number; fps: number };
  ttl?: number;
  expiresAt?: number;
}

export interface CutState {
  state: PlanStatus | "senza-analisi" | "senza-piano";
  analysis: AnalysisSummary | null;
  plan: CutPlan | null;
  /** The plan's etag: what goes back as `If-Match` on an edit. */
  etag?: string | null;
  /** The applied timeline: when its etag is not the one the manual layer was
   *  based on, somebody restored or rewrote it behind the editor's back. */
  timeline?: { etag: string; revision: number } | null;
  media?: MediaInfo;
}

export interface Transcript {
  duration: number;
  words: Word[];
  silences: { start: number; end: number }[];
}
