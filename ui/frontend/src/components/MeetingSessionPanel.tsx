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
  pending: boolean;
  stopMode?: "normal" | "force";
  onStop: (force: boolean) => void;
  onDiarize: (id: string, speakers: number) => void;
}

const outcomeLabels: Record<string, string> = { done: "完成", failed: "失敗", aborted: "已取消" };

const stageLabels: Record<string, string> = {
  segmentation: "發言者辨識",
  retranscription: "重新轉錄",
};

export function MeetingSessionPanel({ active, runtime, sessions, selectedId, onSelect, pending, stopMode, onStop, onDiarize }: Props) {
  const [detail, setDetail] = useState<SessionDetail>();
  const [error, setError] = useState<string>();
  const [requested, setRequested] = useState<string>();
  const [showShort, setShowShort] = useState(false);
  const [resultTab, setResultTab] = useState<"raw" | "speakers">("raw");
  const visibleDetail = detail && detail.session.id === selectedId && detail.session.work_type === "meeting" ? detail : undefined;
  const selectedSummary = sessions.find((session) => session.id === selectedId);
  const source = visibleDetail?.source;
  const count = requested ?? String(source?.requested_speakers ?? "");
  const countValid = Number.isInteger(Number(count)) && Number(count) >= 1 && Number(count) <= 30;
  const speakers = [...(visibleDetail?.speakers?.speakers || [])].sort((a, b) => b.speech_seconds - a.speech_seconds);
  const speechTotal = speakers.reduce((sum, speaker) => sum + speaker.speech_seconds, 0);
  const short = speakers.filter((speaker) => speaker.speech_seconds < 30 || speaker.speech_seconds < speechTotal * .02);
  const prominent = speakers.filter((speaker) => !short.includes(speaker));
  const progress = runtime.status?.diarization;
  const meetingRunning = runtime.running && runtime.work_type === "meeting";
  const runningId = sessionIdFromPath(runtime.session, sessions.map((session) => session.id));
  const processed = progress?.processed_seconds;
  const total = progress?.total_seconds;
  const hasProgress = Number.isFinite(processed) && processed! >= 0;
  const hasTotal = Number.isFinite(total) && total! > 0;
  const percentage = hasProgress && hasTotal ? Math.max(0, Math.min(100, Math.round(processed! / total! * 100))) : undefined;

  useEffect(() => { setRequested(undefined); setShowShort(false); }, [selectedId]);

  useEffect(() => {
    if (!active) return;
    if (!selectedId) {
      setDetail(undefined);
      setError(undefined);
      return;
    }
    let mounted = true;
    let snapshotReceived = false;
    setDetail(undefined);
    setError(undefined);
    void api.session(selectedId).then((value) => {
      if (mounted && !snapshotReceived) setDetail(value);
    }).catch((reason) => {
      if (mounted) setError(reason instanceof Error ? reason.message : "無法讀取會議紀錄");
    });
    const events = new EventSource(`/api/v1/sessions/${encodeURIComponent(selectedId)}/stream`);
    events.addEventListener("snapshot", (raw) => {
      snapshotReceived = true;
      if (mounted) setDetail(JSON.parse((raw as MessageEvent<string>).data) as SessionDetail);
    });
    events.addEventListener("content", (raw) => {
      const event = JSON.parse((raw as MessageEvent<string>).data) as ContentEvent;
      if (mounted) setDetail((current) => current ? applyContentEvent(current, event) : current);
    });
    return () => { mounted = false; events.close(); };
  }, [active, selectedId, runtime.running, selectedSummary?.updated_at]);

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
      {runtime.status?.last_error && <p className="inline-error" role="alert">{runtime.status.last_error}</p>}
      <div className="run-actions">
        <button className="button danger" disabled={pending || !!stopMode} aria-busy={stopMode === "normal"} onClick={() => onStop(false)}>
          {stopMode === "normal" ? "正在停止…" : runtime.mode === "diarize" ? "正常取消" : "正常停止"}
        </button>
        <button className="button ghost-danger" disabled={pending || stopMode === "force"} aria-busy={stopMode === "force"} onClick={() => onStop(true)}>{stopMode === "force" ? "正在中止…" : "強制停止"}</button>
      </div>
      {runningId && runningId !== selectedId && <button className="meeting-short-toggle" type="button" onClick={() => onSelect(runningId)}>查看目前場次</button>}
    </section>}

    <section className="meeting-card" id="meeting-history">
      <div className="meeting-card-heading"><div><p className="eyebrow">MEETING LIBRARY</p><h2>會議結果與歷史</h2></div><span className="meeting-demo-tag">真實場次</span></div>
      {sessions.length === 0 ? <p className="meeting-help">尚無會議紀錄。建立會議設定後即可錄音或匯入音檔。</p> : <>
        <div className="meeting-history-list" aria-label="會議紀錄">
          {sessions.map((session) => <button key={session.id} type="button" className={selectedId === session.id ? "active" : ""}
            aria-pressed={selectedId === session.id} onClick={() => onSelect(session.id)}>
            <strong>{session.course || session.id}</strong>
            <span>{formatDate(session.started_at)} · {formatDuration(session.elapsed)} · 原稿{session.phase ? outcomeLabels[session.phase] || "處理中" : "狀態未知"}</span>
            {session.diarization_status && <span>最近辨識：{outcomeLabels[session.diarization_status.phase || ""] || "處理中"}{session.diarization_status.last_error ? ` · ${session.diarization_status.last_error}` : ""}</span>}
            <span> {session.has_speaker_transcript ? "已有發言者逐字稿" : "尚未辨識發言者"}</span>
          </button>)}
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
        {selectedId && !visibleDetail && !error && <p className="meeting-help">正在載入會議逐字稿…</p>}
        {visibleDetail && <>
          <p className="meeting-help">來源：{source ? `${source.kind === "import" ? "匯入" : "錄音"} · ${source.original_name} · ${formatDuration(source.duration_seconds)}` : "沒有可重跑的完整來源音檔"}</p>
          {!visibleDetail.speaker_transcript?.content && <p className="meeting-help">尚未辨識發言者；原逐字稿已保留。請設定預計人數後開始辨識。</p>}
          <form className="meeting-fields" onSubmit={(event) => { event.preventDefault(); if (countValid && selectedId) onDiarize(selectedId, Number(count)); }}>
            <label><span>辨識預計人數（必填）</span><input type="number" min="1" max="30" step="1" required value={count} onChange={(event) => setRequested(event.target.value)} /></label>
            <button className="button primary" type="submit" disabled={pending || runtime.running || !selectedSummary?.has_recording || !countValid}>
              {selectedSummary?.diarization_status?.phase === "aborted" || selectedSummary?.diarization_status?.phase === "failed" ? "重跑辨識" : visibleDetail.speaker_transcript?.content ? "重新辨識" : "開始辨識"}
            </button>
          </form>
          {speakers.length > 0 && <>
            <p>{prominent.filter((speaker) => speaker.id !== "S00").length} 位主要發言者</p>
            <div className="meeting-speaker-list">{prominent.map((speaker) => <div key={speaker.id}><strong>{speaker.id === "S00" ? "未指派" : speaker.id}</strong><span>{formatDuration(speaker.speech_seconds)}</span></div>)}</div>
            {short.length > 0 && <button className="meeting-short-toggle" aria-expanded={showShort} onClick={() => setShowShort(!showShort)}>其他短發言 · {short.length} 位 {showShort ? "收起" : "展開"}</button>}
            {showShort && <div className="meeting-speaker-list short">{short.map((speaker) => <div key={speaker.id}><strong>{speaker.id === "S00" ? "未指派" : speaker.id}</strong><span>{formatDuration(speaker.speech_seconds)}</span></div>)}</div>}
          </>}
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
