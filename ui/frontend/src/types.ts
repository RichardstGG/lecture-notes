export type Phase =
  | "starting"
  | "loading"
  | "recording"
  | "transcribing"
  | "summarizing"
  | "diarizing"
  | "finishing"
  | "done"
  | "failed"
  | "aborted"
  | string;

export interface SessionStatus {
  phase?: Phase;
  diarization?: {
    stage?: "segmentation" | "retranscription" | string;
    processed_seconds?: number;
    total_seconds?: number | null;
    requested_speakers?: number;
    speakers_found?: number;
    actual_speakers?: number | null;
  } | null;
  elapsed?: number;
  transcribed?: number;
  transcribe_lag?: number | null;
  queue?: number;
  sections_total?: number;
  summary_upstream?: string;
  summary_connection?: "ready" | "ok" | "failed";
  sections_summarized?: number;
  llm_busy?: boolean;
  llm_section?: string | null;
  servers?: Record<string, string>;
  errors?: number;
  last_error?: string | null;
  [key: string]: unknown;
}

export interface RuntimeStatus {
  schema_version: number;
  running: boolean;
  pid?: number;
  started_at?: string;
  course?: string;
  session?: string;
  mode?: string;
  work_type?: "lecture" | "meeting" | string;
  status?: SessionStatus;
  [key: string]: unknown;
}

export interface Course {
  file: string;
  id: string;
  name?: string;
  model?: string;
  upstream?: string;
  summary_enabled?: boolean;
  terms?: number;
  error?: string;
}

export interface ModelInfo {
  id: string;
  path: string;
  available: boolean;
  size_bytes?: number;
  disable_thinking?: boolean;
  [key: string]: unknown;
}

export interface ModelGroup {
  selected: string;
  models: ModelInfo[];
  [key: string]: unknown;
}

export interface UpstreamInventory {
  selected: string;
  options: { id: string; name: string; kind: "local" | "api" }[];
}

export interface ModelInventory {
  summary_upstreams?: UpstreamInventory;
  schema_version: number;
  summary: ModelGroup;
  whisper: ModelGroup;
  [key: string]: unknown;
}

export interface AudioSource {
  id: string;
  name: string;
  description?: string;
  state?: string;
  index?: number;
  [key: string]: unknown;
}

export interface DeviceInventory {
  api_version: number;
  current: string;
  default?: string;
  sources: AudioSource[];
}

export interface DeviceTestResult {
  api_version: number;
  source: string;
  message: string;
}

export interface DoctorItem {
  status: string;
  name: string;
  detail: string;
  [key: string]: unknown;
}

export interface DoctorResult {
  api_version: number;
  course?: string;
  microphone_test: boolean;
  summary: {
    ok: number;
    warnings: number;
    failures: number;
  };
  items: DoctorItem[];
}

export interface AudioUpload {
  api_version: number;
  name: string;
  path: string;
  size_bytes: number;
}

export interface CourseDetail {
  api_version: number;
  id: string;
  file: string;
  content: string;
  vocabulary?: CourseVocabulary | null;
}

export interface GlossaryEntry {
  term: string;
  means: string;
  aka: string[];
}

export interface CourseVocabulary {
  terms: string[];
  glossary: GlossaryEntry[];
  ignored_terms?: string[];
}

export interface TermCandidate {
  term: string;
  count: number;
  sections: string[];
  explain: string;
  asr_original: string;
  verified: boolean;
  flags: string[];
  variants: { term: string; count: number; sections: string[]; similarity: number }[];
}

export interface TermCandidatesResponse {
  schema_version: number;
  session: string;
  course: string | null;
  course_id: string | null;
  course_file: string | null;
  whisper_prompt_base: string;
  defined: { terms: number; glossary: number };
  candidates: TermCandidate[];
}

export interface SessionSummary {
  id: string;
  course?: string;
  work_type?: "lecture" | "meeting" | string;
  started_at: string;
  updated_at: string;
  phase?: Phase;
  mode?: string;
  elapsed?: number;
  sections_total?: number;
  summary_upstream?: string;
  summary_connection?: "ready" | "ok" | "failed";
  sections_summarized?: number;
  has_transcript: boolean;
  has_notes: boolean;
  has_recording: boolean;
  has_speaker_transcript?: boolean;
}

export interface ContentFile {
  content: string;
  updated_at?: string;
  size_bytes: number;
}

export interface SessionDetail {
  api_version: number;
  session: SessionSummary;
  transcript: ContentFile;
  notes: ContentFile;
  speaker_transcript?: ContentFile;
}

export interface ContentEvent {
  target: "transcript" | "notes" | "speaker_transcript";
  operation: "append" | "replace";
  content: string;
  updated_at?: string;
  size_bytes: number;
}

export interface RunRequest {
  course: string;
  input_file?: string;
  model?: string;
  upstream?: string;
  source?: string;
  overrides?: Record<string, unknown>;
}

export interface ActionResponse {
  accepted: boolean;
  operation: string;
  pid?: number;
  completed?: boolean;
  exit_code?: number;
  force?: boolean;
  message: string;
}

export interface ApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    stderr?: string;
  };
}


export interface SharingNetwork {
  api_version: number;
  interface?: string | null;
  advertise_host?: string | null;
  error?: string | null;
}

export interface SharingStatus {
  api_version: number;
  active: boolean;
  session_id?: string;
  work_type?: "lecture" | "meeting" | string;
  url?: string;
  max_online?: number;
  bind_host?: string;
  advertise_host?: string;
  error?: string;
  participants: { id: string; nickname: string; online: boolean }[];
}
