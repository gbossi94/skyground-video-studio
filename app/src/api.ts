import type { CutPlan, CutState, MediaInfo, Transcript } from "./types";

class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly payload: unknown = null) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(
      (payload as { error?: string }).error ?? `richiesta non riuscita (${response.status})`,
      response.status,
      payload,
    );
  }
  return payload as T;
}

export interface ProjectSummary {
  id: string;
  name: string;
  status: string;
  canvas: { duration: number; width: number; height: number; fps: number };
  files: Record<string, string>;
  previewAvailable: boolean;
  /** False for a project created by name that is still waiting for its footage. */
  hasSource?: boolean;
}

export interface Me {
  id: string;
  email: string;
  name: string;
  isAdmin: boolean;
}

export interface Invitation {
  id: string;
  email: string;
  name: string;
  admin: boolean;
  createdAt: string;
  expiresAt: string;
}

export interface JobSummary {
  id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  attempts: number;
  error: string | null;
  result: Record<string, unknown> | null;
  createdAt?: string;
  startedAt?: string | null;
  finishedAt?: string | null;
}

/** A raw video becomes a project in one request: the file is the body. */
async function createProject(slug: string, name: string, file: File, template?: string) {
  const query = template ? `?template=${encodeURIComponent(template)}` : "";
  const response = await fetch(`/api/projects/${encodeURIComponent(slug)}${query}`, {
    method: "PUT",
    body: file,
    headers: {
      "Content-Type": file.type || "video/mp4",
      "X-Skyground-Name": name,
      "X-Skyground-Filename": file.name,
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError((payload as { error?: string }).error ?? "caricamento non riuscito", response.status);
  }
  return payload as ProjectSummary;
}

/** The footage into a project that exists already, with progress: `fetch`
 *  cannot report how much of a request body has gone, XHR can. */
function uploadSource(
  slug: string,
  file: File,
  onProgress: (sent: number, total: number) => void,
): { done: Promise<{ project: ProjectSummary; job: JobSummary }>; cancel: () => void } {
  const request = new XMLHttpRequest();
  const done = new Promise<{ project: ProjectSummary; job: JobSummary }>((resolve, reject) => {
    request.open("PUT", `/api/projects/${encodeURIComponent(slug)}/source`);
    request.setRequestHeader("Content-Type", file.type || "video/mp4");
    request.setRequestHeader("X-Skyground-Filename", file.name);
    request.upload.onprogress = (event) => onProgress(event.loaded, event.total || file.size);
    request.onload = () => {
      let payload: unknown = {};
      try { payload = JSON.parse(request.responseText || "{}"); } catch { /* not JSON */ }
      if (request.status >= 200 && request.status < 300) resolve(payload as { project: ProjectSummary; job: JobSummary });
      else reject(new ApiError((payload as { error?: string }).error ?? `caricamento non riuscito (${request.status})`, request.status, payload));
    };
    request.onerror = () => reject(new ApiError("la connessione si è interrotta durante il caricamento", 0));
    request.onabort = () => reject(new ApiError("caricamento annullato", 0));
    request.send(file);
  });
  return { done, cancel: () => request.abort() };
}

export const api = {
  createEmpty: (name: string) =>
    call<ProjectSummary>("/api/projects", { method: "POST", body: JSON.stringify({ name }) }),
  uploadSource,
  me: () => call<{ user: Me; authMode: string }>("/api/auth/me"),
  changePassword: (currentPassword: string, newPassword: string) =>
    call<{ ok: boolean }>("/api/auth/password", { method: "POST", body: JSON.stringify({ currentPassword, newPassword }) }),
  invitations: () => call<Invitation[]>("/api/invitations"),
  invite: (email: string, name: string, admin: boolean) =>
    call<Invitation & { url: string }>("/api/invitations", { method: "POST", body: JSON.stringify({ email, name, admin }) }),
  revokeInvitation: (id: string) => call<{ revoked: string }>(`/api/invitations/${id}`, { method: "DELETE" }),
  openInvitation: (token: string) =>
    call<{ email: string; name: string; admin: boolean }>(`/api/invitations/open/${encodeURIComponent(token)}`),
  acceptInvitation: (token: string, password: string, name: string) =>
    call<{ user: Me }>(`/api/invitations/open/${encodeURIComponent(token)}`, { method: "POST", body: JSON.stringify({ password, name }) }),
  createProject,
  fullCut: (slug: string) =>
    call<JobSummary>(`/api/projects/${slug}/cut/full`, { method: "POST", body: "{}" }),
  job: (slug: string, id: string) => call<JobSummary>(`/api/projects/${slug}/jobs/${id}`),
  jobs: (slug: string) => call<JobSummary[]>(`/api/projects/${slug}/jobs?limit=5`),
  projects: () => call<ProjectSummary[]>("/api/projects"),
  cut: (slug: string) => call<CutState>(`/api/projects/${slug}/cut`),
  transcript: (slug: string) => call<Transcript>(`/api/projects/${slug}/cut/transcript`),
  /** `keepAnswers: false` throws away the choices made by hand and proposes
   *  from the material alone — the only way back from a choice that turned
   *  out to be wrong. */
  propose: (slug: string, keepAnswers = true) =>
    call<{ plan: CutPlan }>(`/api/projects/${slug}/cut/propose`, {
      method: "POST",
      body: JSON.stringify({ keepAnswers }),
    }),
  answer: (slug: string, questionId: string, option: string) =>
    call<{ plan: CutPlan }>(`/api/projects/${slug}/cut/questions/${encodeURIComponent(questionId)}`, {
      method: "POST",
      body: JSON.stringify({ option }),
    }),
  apply: (slug: string, options: { etag?: string | null; rebuild?: boolean; render?: boolean } = {}) =>
    call<{ applied: boolean; clips: number; duration: number; problems: string[]; revision: number;
      etag: string | null; job?: JobSummary }>(
      `/api/projects/${slug}/cut/apply`,
      {
        method: "POST",
        body: JSON.stringify({ rebuild: options.rebuild ?? false, render: options.render ?? true }),
        headers: options.etag ? { "If-Match": `"${options.etag}"` } : {},
      },
    ),
  /** The cut as it is on the timeline. `etag` is the plan this edit started
   *  from: a stale one comes back as a 409 with the current plan, never as a
   *  silent overwrite. */
  edits: (slug: string, kept: { first: number; last: number; start?: number; end?: number }[], etag?: string | null) =>
    call<EditResult>(`/api/projects/${slug}/cut/edits`, {
      method: "POST",
      body: JSON.stringify({ kept }),
      headers: etag ? { "If-Match": `"${etag}"` } : {},
    }),
  editsFromTimeline: (slug: string) =>
    call<EditResult>(`/api/projects/${slug}/cut/edits`, {
      method: "POST",
      body: JSON.stringify({ fromTimeline: true }),
    }),
  media: (slug: string) => call<MediaInfo>(`/api/projects/${slug}/cut/media`),
  requestMedia: (slug: string) =>
    call<JobSummary>(`/api/projects/${slug}/cut/media`, { method: "POST", body: "{}" }),
  analyze: (slug: string) =>
    call<{ id: string; status: string }>(`/api/projects/${slug}/cut/analyze`, {
      method: "POST",
      body: "{}",
    }),
};

export interface EditResult {
  state: string;
  plan: CutPlan;
  etag: string;
  notes: string[];
}

/** What a 409 carries: the plan somebody else saved, and its etag. */
export interface ConflictPayload {
  current: CutPlan;
  etag: string;
}

export { ApiError };
