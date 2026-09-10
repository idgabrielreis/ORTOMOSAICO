/** Cliente da API. Todas as chamadas passam pelo mesmo origin (ver rewrites). */

export type ProjectStatus =
  | "created" | "scanning" | "ready" | "processing" | "completed" | "failed";

export interface Project {
  id: string;
  name: string;
  description: string;
  status: ProjectStatus;
  source_path: string | null;
  source_paths: string[];
  source_kind: string;
  quality: string;
  output_epsg: number | null;
  target_gsd_cm: number | null;
  summary: DatasetSummary;
  created_at: string;
  updated_at: string;
}

export interface DatasetSummary {
  total_files?: number;
  folders?: number;
  folder_names?: string[];
  valid_images?: number;
  invalid_images?: number;
  duplicate_images?: number;
  images_with_gps?: number;
  images_without_gps?: number;
  mean_relative_altitude_m?: number | null;
  cameras?: { model: string; count: number }[];
  bands?: { band: string; count: number }[];
  resolutions?: { size: string; count: number }[];
  area_ha?: number | null;
  gsd_cm?: number | null;
  epsg?: number | null;
  utm_zone?: string;
  center?: [number, number];
  bounds_wgs84?: [number, number, number, number] | null;
  hull_wgs84?: [number, number][];
  captured_from?: string | null;
  captured_to?: string | null;
  invalid_samples?: { file: string; reason: string }[];
  duplicate_samples?: { file: string; duplicate_of: string }[];
  root?: string;
  last_result?: Record<string, unknown>;
}

export interface Job {
  id: string;
  project_id: string;
  kind: string;
  engine: string;
  status: "queued" | "running" | "succeeded" | "failed" | "canceled";
  stage: number;
  stage_label: string;
  progress: number;
  images_done: number;
  images_total: number;
  warnings: string[];
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobEvent {
  id: string;
  status: Job["status"];
  stage: number;
  stage_label: string;
  progress: number;
  images_done: number;
  images_total: number;
  warnings: string[];
  error: string | null;
  elapsed_seconds: number | null;
  eta_seconds: number | null;
  resources: {
    cpu_percent: number | null;
    cpu_count?: number;
    memory_percent: number | null;
    memory_used_gb: number | null;
    memory_total_gb: number | null;
    gpus: { name: string; utilization_percent: number; memory_used_mb: number;
            memory_total_mb: number }[];
  };
}

export interface Quality {
  value: string;
  label: string;
  summary: string;
}

export interface Engine {
  name: string;
  description: string;
  precision: string;
  available: boolean;
  reason: string;
}

export interface DashboardStats {
  projects_total: number;
  projects_processing: number;
  projects_completed: number;
  images_total: number;
  area_ha_total: number;
  orthomosaics: number;
  active_jobs: { id: string; project_id: string; progress: number; stage_label: string }[];
  recent_projects: {
    id: string; name: string; status: ProjectStatus;
    images: number | null; area_ha: number | null; updated_at: string;
  }[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail?.detail ?? `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const api = {
  stats: () => request<DashboardStats>("/api/stats"),
  systemInfo: () =>
    request<{ engines: Engine[]; qualities: Quality[]; queue_backend: string }>(
      "/api/system/info",
    ),
  browse: (path?: string) =>
    request<{ path: string; parent: string; entries: { name: string; path: string }[] }>(
      `/api/system/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),

  projects: () => request<Project[]>("/api/projects"),
  project: (id: string) => request<Project>(`/api/projects/${id}`),
  createProject: (body: { name: string; description?: string; quality?: string;
                          output_epsg?: number | null; target_gsd_cm?: number | null }) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  deleteProject: (id: string) => request<void>(`/api/projects/${id}`, { method: "DELETE" }),

  /** Uma ou várias pastas; todas formam um único dataset. */
  scan: (id: string, paths: string[]) =>
    request<{ job_id: string; roots: string[] }>(`/api/projects/${id}/scan`, {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),
  summary: (id: string) => request<DatasetSummary>(`/api/projects/${id}/summary`),
  folders: (id: string) => request<{ folder: string; images: number }[]>(
    `/api/projects/${id}/folders`),
  jobs: (id: string) => request<Job[]>(`/api/projects/${id}/jobs`),
  job: (jobId: string) => request<Job>(`/api/jobs/${jobId}`),
  jobLog: (jobId: string) => fetch(`/api/jobs/${jobId}/log`).then((r) => r.text()),
  cancelJob: (jobId: string) => request<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" }),
  startProcessing: (id: string, body: { engine?: string; quality?: string }) =>
    request<Job>(`/api/projects/${id}/jobs`, { method: "POST", body: JSON.stringify(body) }),

  rasterInfo: (id: string) =>
    request<{ epsg: number; width: number; height: number; gsd_m: number;
              bounds_wgs84: [number, number, number, number] }>(
      `/api/projects/${id}/exports/info`),
  tileJson: (id: string) =>
    request<{ tiles: string[]; bounds: [number, number, number, number];
              minzoom: number; maxzoom: number }>(`/api/projects/${id}/tilejson.json`),
};

/** Envia as imagens do navegador preservando a árvore de pastas do voo. */
export async function uploadFolder(
  projectId: string,
  files: File[],
  onProgress: (sent: number, total: number) => void,
  batchSize = 25,
): Promise<{ job_id: string }> {
  let sent = 0;
  for (let index = 0; index < files.length; index += batchSize) {
    const batch = files.slice(index, index + batchSize);
    const form = new FormData();
    for (const file of batch) {
      form.append("files", file);
      // webkitRelativePath preserva CAMERA_01/DJI_0001.JPG
      form.append("relative_paths", (file as File & { webkitRelativePath?: string })
        .webkitRelativePath || file.name);
    }
    const last = index + batchSize >= files.length;
    form.append("finalize", String(last));
    const response = await fetch(`/api/projects/${projectId}/upload`, {
      method: "POST",
      body: form,
    });
    if (!response.ok) throw new Error(`falha no envio do lote ${index / batchSize + 1}`);
    const payload = await response.json();
    sent += batch.length;
    onProgress(sent, files.length);
    if (last) return payload;
  }
  throw new Error("nenhum arquivo enviado");
}

export function subscribeJob(jobId: string, onEvent: (event: JobEvent) => void): () => void {
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as JobEvent);
    } catch {
      /* mensagens de keep-alive */
    }
  };
  source.addEventListener("done", () => source.close());
  return () => source.close();
}
