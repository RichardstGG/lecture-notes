import { describe, expect, it } from "vitest";
import type { SessionDetail } from "./types";
import { applyContentEvent, formatClock, formatDuration, progressFor, sessionIdFromPath } from "./utils";

describe("frontend state helpers", () => {
  it("formats lecture durations", () => {
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(3661)).toBe("1:01:01");
    expect(formatDuration(undefined)).toBe("—");
  });

  it("formats a fixed-width phase clock", () => {
    expect(formatClock(0)).toBe("00:00");
    expect(formatClock(65.9)).toBe("01:05");
    expect(formatClock(3661)).toBe("01:01:01");
  });

  it("bounds transcription progress", () => {
    expect(progressFor({ elapsed: 100, transcribed: 25 })).toBe(25);
    expect(progressFor({ elapsed: 10, transcribed: 15 })).toBe(100);
    expect(progressFor({ elapsed: 0, transcribed: 4 })).toBe(0);
  });

  it("applies append and replacement session events", () => {
    const detail: SessionDetail = {
      api_version: 1,
      session: {
        id: "a", started_at: "now", updated_at: "now",
        has_transcript: true, has_notes: true, has_recording: false,
      },
      transcript: { content: "first", size_bytes: 5 },
      notes: { content: "old", size_bytes: 3 },
    };
    expect(applyContentEvent(detail, {
      target: "transcript", operation: "append", content: " second", size_bytes: 12,
    }).transcript.content).toBe("first second");
    expect(applyContentEvent(detail, {
      target: "notes", operation: "replace", content: "new", size_bytes: 3,
    }).notes.content).toBe("new");
  });

  it("extracts session ids from POSIX and Windows paths", () => {
    expect(sessionIdFromPath("/tmp/outputs/course_20260918")).toBe("course_20260918");
    expect(sessionIdFromPath("C:\\notes\\outputs\\course_20260918")).toBe("course_20260918");
  });
});
