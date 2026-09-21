export type Phase =
  | "starting"
  | "loading"
  | "recording"
  | "transcribing"
  | "summarizing"
  | "finishing"
  | "done"
  | "failed"
  | "aborted"
  | string;

export interface SessionStatus {
  phase?: Phase;
  elapsed?: number;
  transcribed?: number;
  transcribe_lag?: number | null;
  queue?: number;
  sections_total?: number;
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
  status?: SessionStatus;
  [key: string]: unknown;
}

export interface Course {
  file: string;
  id: string;
  name?: string;
  model?: string;
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

export interface ModelInventory {
  schema_version: number;
  summary: ModelGroup;
  whisper: ModelGroup;
  [key: string]: unknown;
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
}

export interface SessionSummary {
  id: string;
  course?: string;
  started_at: string;
  updated_at: string;
  phase?: Phase;
  mode?: string;
  elapsed?: number;
  sections_total?: number;
  sections_summarized?: number;
  has_transcript: boolean;
  has_notes: boolean;
  has_recording: boolean;
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
}

export interface ContentEvent {
  target: "transcript" | "notes";
  operation: "append" | "replace";
  content: string;
  updated_at?: string;
  size_bytes: number;
}

export interface RunRequest {
  course: string;
  input_file?: string;
  model?: string;
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
