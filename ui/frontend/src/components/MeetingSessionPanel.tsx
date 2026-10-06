import { useEffect, useState } from "react";
import { api } from "../api";
import type { ContentEvent, RuntimeStatus, SessionDetail, SessionSummary } from "../types";
import { applyContentEvent, formatDate, formatDuration, sessionIdFromPath } from "../utils";
import { MarkdownPane } from "./MarkdownPane";

interface Props {
  active: boolean;
  runtime: RuntimeStatus;
  sessions: SessionSummary[];
  selectedId?: string;
  onSelect: (id: string) => void;
}

const stageLabels: Record<string, string> = {
  segmentation: "發言者辨識",
  retranscription: "重新轉錄",
};

export function MeetingSessionPanel({ active, runtime, sessions, selectedId, onSelect }: Props) {
  const [detail, setDetail] = useState<SessionDetail>();
  const [error, setError] = useState<string>();
  const [resultTab, setResultTab] = useState<"raw" | "speakers">("raw");
  const visibleDetail = detail && detail.session.id === selectedId && detail.session.work_type === "meeting" ? detail : undefined;
  const progress = runtime.status?.diarization;
  const meetingRunning = runtime.running && runtime.work_type === "meeting";
  const runningId = sessionIdFromPath(runtime.session, sessions.map((session) => session.id));
  const processed = progress?.processed_seconds;
  const total = progress?.total_seconds;
  const hasProgress = Number.isFinite(processed) && processed! >= 0;
  const hasTotal = Number.isFinite(total) && total! > 0;
  const percentage = hasProgress && hasTotal ? Math.max(0, Math.min(100, Math.round(processed! / total! * 100))) : undefined;

  useEffect(() => {
    if (!active) return;
    if (!selectedId) {
      setDetail(undefined);
      setError(undefined);
      return;
    }
    let mounted = true;
    setDetail(undefined);
    setError(undefined);
    void api.session(selectedId).then((value) => {
      if (mounted) setDetail(value);
    }).catch((reason) => {
      if (mounted) setError(reason instanceof Error ? reason.message : "無法讀取會議紀錄");
    });
    const events = new EventSource(`/api/v1/sessions/${encodeURIComponent(selectedId)}/stream`);
    events.addEventListener("snapshot", (raw) => {
      if (mounted) setDetail(JSON.parse((raw as MessageEvent<string>).data) as SessionDetail);
    });
    events.addEventListener("content", (raw) => {
      const event = JSON.parse((raw as MessageEvent<string>).data) as ContentEvent;
      if (mounted) setDetail((current) => current ? applyContentEvent(current, event) : current);
    });
    return () => { mounted = false; events.close(); };
  }, [active, selectedId]);

  return <>
    {meetingRunning && <section className="meeting-card" aria-label="目前會議工作">
      <div className="meeting-card-heading"><div><p className="eyebrow">LIVE WORK</p><h2>{runtime.course || "目前會議工作"}</h2></div></div>
      <p className="meeting-help">{runtime.mode === "diarize" || runtime.status?.phase === "diarizing"
        ? `${stageLabels[progress?.stage || ""] || "辨識發言者"}進行中` : "錄音或轉錄進行中"}。切換工作台不會停止背景工作。</p>
      {hasProgress && <div className="meeting-progress" {...(percentage !== undefined ? {
        role: "progressbar", "aria-label": "會議辨識進度", "aria-valuenow": percentage,
        "aria-valuemin": 0, "aria-valuemax": 100,
      } : {})}>
        <strong>{stageLabels[progress?.stage || ""] || "處理中"} · 已處理 {formatDuration(processed)}{hasTotal ? ` / ${formatDuration(total!)}` : ""}</strong>
        {percentage !== undefined && <div className="meeting-progress-track"><span style={{ width: `${percentage}%` }} /></div>}
        <p>要求 {progress?.requested_speakers ?? "—"} 位 · 已找到 {progress?.speakers_found ?? "—"} 個代號</p>
      </div>}
      {runningId && runningId !== selectedId && <button className="meeting-short-toggle" type="button" onClick={() => onSelect(runningId)}>查看目前場次</button>}
    </section>}

    <section className="meeting-card" id="meeting-history">
      <div className="meeting-card-heading"><div><p className="eyebrow">MEETING LIBRARY</p><h2>會議結果與歷史</h2></div><span className="meeting-demo-tag">真實場次</span></div>
      {sessions.length === 0 ? <p className="meeting-help">尚無會議紀錄。會議錄音與辨識功能仍待後端串接。</p> : <>
        <div className="meeting-history-list" aria-label="會議紀錄">
          {sessions.map((session) => <button key={session.id} type="button" className={selectedId === session.id ? "active" : ""}
            aria-pressed={selectedId === session.id} onClick={() => onSelect(session.id)}>
            <strong>{session.course || session.id}</strong>
            <span>{formatDate(session.started_at)} · {session.has_speaker_transcript ? "已有發言者逐字稿" : "尚未辨識發言者"}</span>
          </button>)}
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
        {selectedId && !visibleDetail && !error && <p className="meeting-help">正在載入會議逐字稿…</p>}
        {visibleDetail && <>
          <div className="meeting-demo-controls" role="tablist" aria-label="會議逐字稿">
            <button role="tab" aria-selected={resultTab === "raw"} className={resultTab === "raw" ? "active" : ""} onClick={() => setResultTab("raw")}>原逐字稿</button>
            <button role="tab" aria-selected={resultTab === "speakers"} className={resultTab === "speakers" ? "active" : ""} onClick={() => setResultTab("speakers")}>帶發言者逐字稿</button>
          </div>
          <div className="meeting-transcript meeting-real-transcript">
            <MarkdownPane content={resultTab === "raw" ? visibleDetail.transcript.content : visibleDetail.speaker_transcript?.content}
              empty={resultTab === "raw" ? "原逐字稿尚未產生。" : "尚未產生帶發言者逐字稿。"} />
          </div>
        </>}
      </>}
    </section>
  </>;
}
