import type { CutPlan, CutState, Transcript } from "./types";

class ApiError extends Error {
  constructor(message: string, readonly status: number) {
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
    throw new ApiError((payload as { error?: string }).error ?? "richiesta non riuscita", response.status);
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
}

export interface JobSummary {
  id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  attempts: number;
  error: string | null;
  result: Record<string, unknown> | null;
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

export const api = {
  me: () => call<{ user: { email: string }; authMode: string }>("/api/auth/me"),
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
  apply: (slug: string) =>
    call<{ applied: boolean; clips: number; duration: number; problems: string[] }>(
      `/api/projects/${slug}/cut/apply`,
      { method: "POST" },
    ),
  analyze: (slug: string) =>
    call<{ id: string; status: string }>(`/api/projects/${slug}/cut/analyze`, {
      method: "POST",
      body: "{}",
    }),
};

export { ApiError };
