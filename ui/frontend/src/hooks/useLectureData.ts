import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type {
  ContentEvent,
  Course,
  RuntimeStatus,
  SessionDetail,
  SessionSummary,
} from "../types";
import { applyContentEvent } from "../utils";

const idleStatus: RuntimeStatus = { schema_version: 1, running: false };

export function useLectureData() {
  const [status, setStatus] = useState<RuntimeStatus>(idleStatus);
  const [courses, setCourses] = useState<Course[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [detail, setDetail] = useState<SessionDetail>();
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const previousRunning = useRef(false);

  const refreshSessions = useCallback(async () => {
    const result = await api.sessions();
    setSessions(result);
    setSelectedId((current) => current ?? result[0]?.id);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [nextStatus, nextCourses, nextSessions] = await Promise.all([
        api.status(), api.courses(), api.sessions(),
      ]);
      setStatus(nextStatus);
      setCourses(nextCourses);
      setSessions(nextSessions);
      setSelectedId((current) => current ?? nextSessions[0]?.id);
      setError(undefined);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "載入資料時發生錯誤");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const events = new EventSource("/api/v1/status/stream");
    events.addEventListener("status", (raw) => {
      const next = JSON.parse((raw as MessageEvent<string>).data) as RuntimeStatus;
      setStatus(next);
      setConnected(true);
      setError(undefined);
      if (previousRunning.current && !next.running) void refreshSessions();
      previousRunning.current = next.running;
    });
    events.addEventListener("error", () => setConnected(false));
    events.onopen = () => setConnected(true);
    return () => events.close();
  }, [refreshSessions]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(undefined);
      return;
    }
    let active = true;
    void api.session(selectedId).then((value) => {
      if (active) setDetail(value);
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : "無法讀取紀錄");
    });
    const events = new EventSource(`/api/v1/sessions/${encodeURIComponent(selectedId)}/stream`);
    events.addEventListener("snapshot", (raw) => {
      if (active) setDetail(JSON.parse((raw as MessageEvent<string>).data) as SessionDetail);
    });
    events.addEventListener("content", (raw) => {
      const event = JSON.parse((raw as MessageEvent<string>).data) as ContentEvent;
      if (active) setDetail((current) => current ? applyContentEvent(current, event) : current);
    });
    return () => {
      active = false;
      events.close();
    };
  }, [selectedId]);

  return {
    status, courses, sessions, selectedId, setSelectedId, detail,
    connected, loading, error, setError, refresh, refreshSessions,
  };
}
