import type { ContentEvent, SessionDetail, SessionStatus } from "./types";

export function formatDuration(seconds?: number): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const value = Math.max(0, Math.round(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = value % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

export function formatClock(seconds: number): string {
  const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = value % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

export function formatDate(value?: string): string {
  if (!value) return "日期未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-TW", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function progressFor(status?: SessionStatus): number {
  if (!status) return 0;
  const elapsed = Number(status.elapsed ?? 0);
  const transcribed = Number(status.transcribed ?? 0);
  if (elapsed <= 0) return 0;
  return Math.min(100, Math.max(0, (transcribed / elapsed) * 100));
}

export function applyContentEvent(
  current: SessionDetail,
  event: ContentEvent,
): SessionDetail {
  const before = current[event.target];
  return {
    ...current,
    [event.target]: {
      content: event.operation === "append"
        ? before.content + event.content
        : event.content,
      updated_at: event.updated_at,
      size_bytes: event.size_bytes,
    },
  };
}

export function sessionIdFromPath(path?: string): string | undefined {
  if (!path) return undefined;
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean);
  return parts.at(-1);
}
