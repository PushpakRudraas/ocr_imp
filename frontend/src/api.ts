import type { Article, Capabilities, CleanArticle, ReviewItem, Run, Story, StoryGraph, UploadItem } from './types';

const BASE: string = import.meta.env.VITE_API_BASE ?? '';

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

/** FastAPI sends `detail` as a string, or as a list of {loc,msg} for validation errors. */
export function errorMessage(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d))).join('; ');
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, init);
  } catch {
    throw new ApiError('Cannot reach the server. Is the API running?', 0);
  }
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* not json */
    }
    throw new ApiError(errorMessage(body, `Request failed (${res.status})`), res.status);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const getConfig = () => request<Capabilities>('/api/config');
export const listRuns = () => request<Run[]>('/api/runs');
export const getRun = (id: string) => request<Run>(`/api/runs/${id}`);
export const getClean = (id: string) => request<CleanArticle[]>(`/api/runs/${id}/clean`);
export const getStories = (id: string) => request<Story[]>(`/api/runs/${id}/stories`);
export const getReview = (id: string) => request<ReviewItem[]>(`/api/runs/${id}/review`);
export const getArticle = (id: string, eventId: string) => request<Article>(`/api/runs/${id}/articles/${eventId}`);
export const getStoryGraph = (id: string, storyId: string) => request<StoryGraph>(`/api/runs/${id}/stories/${encodeURIComponent(storyId)}/graph`);
/** Queue the enrichment phase for a run whose extraction is done. */
export const enrichRun = (id: string) => request<Run>(`/api/runs/${id}/enrich`, { method: 'POST' });
export const deleteRun = (id: string) => request<void>(`/api/runs/${id}`, { method: 'DELETE' });

/** Build the multipart body. Files and the `papers` metadata list must be in the same order. */
export function buildUploadForm(runDate: string, items: UploadItem[], enrich = false): FormData {
  const form = new FormData();
  form.append('run_date', runDate);
  form.append('enrich', enrich ? 'true' : 'false');
  form.append(
    'papers',
    JSON.stringify(items.map(({ name, language, edition, reputation }) => ({ name: name.trim(), language, edition: edition.trim() || 'main', reputation }))),
  );
  for (const it of items) form.append('files', it.file, it.file.name);
  return form;
}

/** XHR rather than fetch so we can report upload progress for large PDFs. */
export function createRun(runDate: string, items: UploadItem[], enrich = false, onProgress?: (fraction: number) => void): Promise<Run> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${BASE}/api/runs`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded / e.total);
    };
    xhr.onerror = () => reject(new ApiError('Cannot reach the server. Is the API running?', 0));
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not json */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as Run);
      else reject(new ApiError(errorMessage(body, `Upload failed (${xhr.status})`), xhr.status));
    };
    xhr.send(buildUploadForm(runDate, items, enrich));
  });
}
