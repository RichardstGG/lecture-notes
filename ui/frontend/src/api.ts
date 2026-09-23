import type {
  ActionResponse,
  AudioUpload,
  ApiErrorEnvelope,
  Course,
  CourseDetail,
  CourseVocabulary,
  DeviceInventory,
  DeviceTestResult,
  DoctorResult,
  ModelInventory,
  RunRequest,
  RuntimeStatus,
  SessionDetail,
  SessionSummary,
} from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code = "request_failed", status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: init?.body
        ? { "Content-Type": "application/json", ...init.headers }
        : init?.headers,
    });
  } catch {
    throw new ApiError("無法連線到本機服務", "network_error");
  }

  if (!response.ok) {
    let body: ApiErrorEnvelope = {};
    try {
      body = (await response.json()) as ApiErrorEnvelope;
    } catch {
      // Preserve the stable fallback below for non-JSON proxy errors.
    }
    throw new ApiError(
      body.error?.message ?? `請求失敗（HTTP ${response.status}）`,
      body.error?.code ?? "request_failed",
      response.status,
    );
  }
  return (await response.json()) as T;
}

async function uploadAudio(file: File): Promise<AudioUpload> {
  let response: Response;
  try {
    response = await fetch(
      `/api/v1/audio-uploads?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", body: file },
    );
  } catch {
    throw new ApiError("無法連線到本機服務", "network_error");
  }

  if (!response.ok) {
    let body: ApiErrorEnvelope = {};
    try {
      body = (await response.json()) as ApiErrorEnvelope;
    } catch {
      // Preserve the stable fallback below for non-JSON proxy errors.
    }
    throw new ApiError(
      body.error?.message ?? `檔案準備失敗（HTTP ${response.status}）`,
      body.error?.code ?? "upload_failed",
      response.status,
    );
  }
  return (await response.json()) as AudioUpload;
}

export const api = {
  status: () => request<RuntimeStatus>("/api/v1/status"),
  courses: () => request<Course[]>("/api/v1/courses"),
  models: () => request<ModelInventory>("/api/v1/models"),
  devices: () => request<DeviceInventory>("/api/v1/devices"),
  selectDevice: (source: string) => request<DeviceInventory>("/api/v1/devices/current", {
    method: "PUT",
    body: JSON.stringify({ source }),
  }),
  testDevice: (source: string) => request<DeviceTestResult>("/api/v1/devices/test", {
    method: "POST",
    body: JSON.stringify({ source }),
  }),
  doctor: (course?: string, mic = false) => {
    const query = new URLSearchParams();
    if (course) query.set("course", course);
    if (mic) query.set("mic", "true");
    const encoded = query.toString();
    const suffix = encoded ? `?${encoded}` : "";
    return request<DoctorResult>(`/api/v1/doctor${suffix}`);
  },
  uploadAudio,
  course: (id: string) => request<CourseDetail>(
    `/api/v1/courses/${encodeURIComponent(id)}`,
  ),
  createCourse: (id: string) => request<CourseDetail>("/api/v1/courses", {
    method: "POST",
    body: JSON.stringify({ id }),
  }),
  updateCourse: (id: string, content: string) => request<CourseDetail>(
    `/api/v1/courses/${encodeURIComponent(id)}`,
    { method: "PUT", body: JSON.stringify({ content }) },
  ),
  updateCourseVocabulary: (id: string, vocabulary: CourseVocabulary) => request<CourseDetail>(
    `/api/v1/courses/${encodeURIComponent(id)}/vocabulary`,
    { method: "PUT", body: JSON.stringify(vocabulary) },
  ),
  sessions: () => request<SessionSummary[]>("/api/v1/sessions"),
  session: (id: string) => request<SessionDetail>(`/api/v1/sessions/${encodeURIComponent(id)}`),
  start: (payload: RunRequest) => request<ActionResponse>("/api/v1/runs", {
    method: "POST",
    body: JSON.stringify(payload),
  }),
  stop: (force: boolean) => request<ActionResponse>("/api/v1/runs/stop", {
    method: "POST",
    body: JSON.stringify({ force }),
  }),
  summarize: (id: string, redo?: string) => request<ActionResponse>(
    `/api/v1/sessions/${encodeURIComponent(id)}/summarize`,
    { method: "POST", body: JSON.stringify(redo ? { redo } : {}) },
  ),
};
