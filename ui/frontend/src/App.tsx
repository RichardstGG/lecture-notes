import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { Icon } from "./components/Icon";
import { CourseEditor } from "./components/CourseEditor";
import { MarkdownPane } from "./components/MarkdownPane";
import { LocalSettings } from "./components/LocalSettings";
import { RunPanel } from "./components/RunPanel";
import { useLectureData } from "./hooks/useLectureData";
import { zhTW as t } from "./i18n/zh-TW";
import type { SessionDetail } from "./types";
import { formatClock, formatDate, formatDuration, progressFor, sessionIdFromPath } from "./utils";

const phaseLabels: Record<string, string> = {
  starting: "啟動中", loading: "載入模型", recording: "錄音中",
  transcribing: "轉錄中", summarizing: "整理筆記", finishing: "收尾中",
  done: "已完成", failed: "失敗", aborted: "已中止",
};

export default function App() {
  const data = useLectureData();
  const [tab, setTab] = useState<"transcript" | "notes">("transcript");
  const [actionMessage, setActionMessage] = useState<string>();
  const [summarizing, setSummarizing] = useState(false);
  const [phaseStartedAt, setPhaseStartedAt] = useState(() => Date.now());
  const [clockNow, setClockNow] = useState(() => Date.now());
  const runningId = sessionIdFromPath(
    data.status.session, data.sessions.map((session) => session.id),
  );
  const visibleDetail: SessionDetail | undefined = data.detail;
  const phase = data.status.status?.phase;
  const progress = progressFor(data.status.status);
  const phaseKey = data.status.running ? (phase || data.status.mode || "running") : "idle";
  const phaseSeconds = Math.floor((clockNow - phaseStartedAt) / 1000);

  useEffect(() => {
    const current = Date.now();
    setPhaseStartedAt(current);
    setClockNow(current);
  }, [phaseKey]);

  useEffect(() => {
    if (!data.status.running) return;
    const timer = window.setInterval(() => setClockNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [data.status.running]);

  useEffect(() => {
    if (runningId && data.sessions.some((session) => session.id === runningId)) {
      data.setSelectedId(runningId);
    }
  }, [runningId, data.sessions, data.setSelectedId]);

  const stats = useMemo(() => [
    ["已處理音訊", formatDuration(data.status.status?.transcribed)],
    ["待轉錄片段", String(data.status.status?.queue ?? 0)],
    ["筆記進度", `${data.status.status?.sections_summarized ?? 0} / ${data.status.status?.sections_total ?? 0}`],
  ], [data.status.status]);

  async function summarize() {
    if (!data.selectedId) return;
    setSummarizing(true);
    data.setError(undefined);
    try {
      const result = await api.summarize(data.selectedId);
      setActionMessage(result.message);
      await data.refresh();
    } catch (reason) {
      data.setError(reason instanceof Error ? reason.message : "無法啟動總結");
    } finally {
      setSummarizing(false);
    }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Icon name="wave" /></div><div><strong>{t.brand}</strong><span>{t.brandSubtitle}</span></div></div>
      <nav>
        <a className="active" href="#live"><Icon name="wave" />課堂工作台</a>
        <a href="#history"><Icon name="history" />歷史紀錄</a>
        <a href="#courses"><Icon name="book" />課程設定</a>
        <a href="#local-settings"><Icon name="mic" />本機設定</a>
      </nav>
      <div className="privacy-note"><span>LOCAL</span><p>錄音與筆記只保存在這台電腦。</p></div>
    </aside>

    <main>
      <header className="topbar">
        <div><p className="eyebrow">WORKSPACE</p><h1>課堂工作台</h1></div>
        <div className={`connection ${data.connected ? "online" : "offline"}`}><i />{data.connected ? "本機服務已連線" : "正在重新連線"}</div>
      </header>

      {(data.error || actionMessage) && <div className={data.error ? "notice error" : "notice"}>
        <span>{data.error || actionMessage}</span><button onClick={() => { data.setError(undefined); setActionMessage(undefined); }}>關閉</button>
      </div>}

      <section className={`hero ${data.status.running ? "running" : "idle"}`} id="live">
        <div className="hero-heading">
          <div className="live-orb"><span /><Icon name={data.status.mode === "file" ? "file" : "mic"} /></div>
          <div>
            <p className="eyebrow">{data.status.running ? "NOW PROCESSING" : "READY"}</p>
            <h2>{data.status.running ? data.status.course || "未命名課程" : t.idle}</h2>
            <p>{data.status.running ? `${phaseLabels[phase || ""] || phase || "處理中"} · ${data.status.mode === "file" ? "音檔模式" : "現場錄音"}` : "選擇課程後即可開始錄音，或匯入既有音檔。"}</p>
          </div>
          {data.status.running && <div className="phase-timer" aria-label={`本階段用時 ${formatClock(phaseSeconds)}`}>
            <span>本階段用時</span>
            <strong>{formatClock(phaseSeconds)}</strong>
          </div>}
        </div>

        {data.status.running && <>
          <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
          <div className="stats-row">{stats.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
          {data.status.status?.last_error && <p className="inline-error">{data.status.status.last_error}</p>}
        </>}

        <RunPanel courses={data.courses} devices={data.devices} models={data.models} status={data.status} onChanged={data.refresh} onError={data.setError} />
      </section>

      <div className="workspace-grid">
        <section className="content-card">
          <div className="card-header">
            <div className="tabs">
              <button className={tab === "transcript" ? "active" : ""} onClick={() => setTab("transcript")}>{t.transcript}</button>
              <button className={tab === "notes" ? "active" : ""} onClick={() => setTab("notes")}>{t.notes}</button>
            </div>
            {visibleDetail && <span className="updated">更新於 {formatDate(visibleDetail[tab].updated_at)}</span>}
          </div>
          <div className="document-pane">
            <MarkdownPane content={visibleDetail?.[tab].content} empty={tab === "transcript" ? t.emptyTranscript : t.emptyNotes} />
          </div>
        </section>

        <aside className="history-card" id="history">
          <div className="card-title"><div><p className="eyebrow">LIBRARY</p><h2>{t.history}</h2></div><button title={t.refresh} onClick={() => void data.refreshSessions()}><Icon name="refresh" /></button></div>
          <div className="session-list">
            {data.loading && <p className="empty-list">正在載入…</p>}
            {!data.loading && data.sessions.length === 0 && <p className="empty-list">還沒有課堂紀錄。</p>}
            {data.sessions.map((session) => <button className={data.selectedId === session.id ? "session active" : "session"} onClick={() => data.setSelectedId(session.id)} key={session.id}>
              <span className="session-icon"><Icon name="book" /></span>
              <span className="session-copy"><strong>{session.course || session.id}</strong><small>{formatDate(session.started_at)} · {formatDuration(session.elapsed)}</small></span>
              <span className={`phase-dot ${session.phase || "unknown"}`} title={phaseLabels[session.phase || ""] || session.phase} />
            </button>)}
          </div>
          {visibleDetail && !visibleDetail.session.has_notes && visibleDetail.session.has_transcript && <button className="button secondary full" disabled={summarizing || data.status.running} onClick={() => void summarize()}>{summarizing ? "啟動中…" : "補做課堂筆記"}</button>}
        </aside>
      </div>

      <CourseEditor
        courses={data.courses}
        onChanged={data.refresh}
        onError={data.setError}
        onMessage={setActionMessage}
      />

      <LocalSettings
        courses={data.courses}
        devices={data.devices}
        devicesError={data.devicesError}
        devicesLoading={data.devicesLoading}
        onDevicesChanged={data.setDevices}
        onRefreshDevices={data.refreshDevices}
        onError={data.setError}
        onMessage={setActionMessage}
      />
    </main>
  </div>;
}
