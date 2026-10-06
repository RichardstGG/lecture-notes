import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { Icon } from "./components/Icon";
import { CourseEditor } from "./components/CourseEditor";
import { MarkdownPane } from "./components/MarkdownPane";
import { LocalSettings } from "./components/LocalSettings";
import { SharePanel } from "./components/SharePanel";
import { SummaryActions } from "./components/SummaryActions";
import { RunPanel } from "./components/RunPanel";
import { TermCandidates } from "./components/TermCandidates";
import { MeetingWorkbench } from "./components/MeetingWorkbench";
import { WorkbenchNavigation, navigationFromHash, type Navigation } from "./components/WorkbenchNavigation";
import { useLectureData } from "./hooks/useLectureData";
import { zhTW as t } from "./i18n/zh-TW";
import type { SessionDetail, TermCandidatesResponse } from "./types";
import { formatClock, formatDate, formatDuration, progressFor, sessionIdFromPath } from "./utils";

const phaseLabels: Record<string, string> = {
  starting: "啟動中", loading: "載入模型", recording: "錄音中",
  transcribing: "轉錄中", summarizing: "整理筆記", diarizing: "辨識發言者", finishing: "收尾中",
  done: "已完成", failed: "失敗", aborted: "已中止",
};

export default function App() {
  const data = useLectureData();
  const [navigation, setNavigation] = useState(() => navigationFromHash(window.location.hash));
  const page = navigation.page;
  const lectureSessions = useMemo(() => data.sessions.filter((session) => !session.work_type || session.work_type === "lecture"), [data.sessions]);
  const [tab, setTab] = useState<"transcript" | "notes" | "terms">("transcript");
  const [termData, setTermData] = useState<TermCandidatesResponse>();
  const [actionMessage, setActionMessage] = useState<string>();
  const [phaseStartedAt, setPhaseStartedAt] = useState(() => Date.now());
  const [clockNow, setClockNow] = useState(() => Date.now());
  const runningId = sessionIdFromPath(
    data.status.session, data.sessions.map((session) => session.id),
  );
  const visibleDetail: SessionDetail | undefined = !data.detail?.session.work_type || data.detail.session.work_type === "lecture" ? data.detail : undefined;
  const runningWorkbench = !data.status.work_type || data.status.work_type === "lecture" ? "lecture"
    : data.status.work_type === "meeting" ? "meeting" : undefined;

  function navigate(next: Navigation) {
    if (window.location.hash !== `#${next.target}`) window.history.pushState(null, "", `#${next.target}`);
    setNavigation(next);
  }

  useEffect(() => {
    const followHash = () => setNavigation(navigationFromHash(window.location.hash));
    window.addEventListener("hashchange", followHash);
    window.addEventListener("popstate", followHash);
    return () => {
      window.removeEventListener("hashchange", followHash);
      window.removeEventListener("popstate", followHash);
    };
  }, []);

  useEffect(() => {
    if (!window.location.hash) return;
    if (navigation.target === "live" || navigation.target === "meeting-live") {
      window.scrollTo({ top: 0 });
      return;
    }
    document.getElementById(navigation.target)?.scrollIntoView({ block: "start" });
  }, [navigation]);

  useEffect(() => {
    setTermData(undefined);
    if (!data.selectedId) return;
    let active = true;
    void api.termCandidates(data.selectedId).then((result) => {
      if (active) setTermData(result);
    }).catch((reason) => {
      if (active) data.setError(reason instanceof Error ? reason.message : "無法讀取術語候選");
    });
    return () => { active = false; };
  }, [data.selectedId, data.setError]);
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
    if (runningId && lectureSessions.some((session) => session.id === runningId)) {
      data.setSelectedId(runningId);
    }
  }, [runningId, lectureSessions, data.setSelectedId]);

  const stats = useMemo(() => [
    ["已處理音訊", formatDuration(data.status.status?.transcribed)],
    ["待轉錄片段", String(data.status.status?.queue ?? 0)],
    ["摘要上游", data.status.status?.summary_upstream || "local"],
    ["API 連線", ({ ready: "待請求", ok: "正常", failed: "失敗" } as Record<string, string>)[data.status.status?.summary_connection || ""] || "—"],
    ["筆記進度", `${data.status.status?.sections_summarized ?? 0} / ${data.status.status?.sections_total ?? 0}`],
  ], [data.status.status]);

  return <div className={`app-shell${page === "meeting" ? " meeting-theme" : ""}`}>
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Icon name="wave" /></div><div><strong>{t.brand}</strong><span>{t.brandSubtitle}</span></div></div>
      <WorkbenchNavigation navigation={navigation} onNavigate={navigate} />
      <div className="privacy-note"><span>LOCAL</span><p>錄音與筆記只保存在這台電腦。</p></div>
    </aside>

    <main>
      <header className="topbar">
        <div><p className="eyebrow">{page === "local-settings" ? "THIS COMPUTER" : "WORKSPACE"}</p><h1>{{ lecture: "課堂工作台", meeting: "會議工作台", "local-settings": "本機設定" }[page]}</h1></div>
        <div className={`connection ${data.connected ? "online" : "offline"}`}><i />{data.connected ? "本機服務已連線" : "正在重新連線"}</div>
      </header>

      {(data.error || actionMessage) && <div className={data.error ? "notice error" : "notice"}>
        <span>{data.error || actionMessage}</span><button onClick={() => { data.setError(undefined); setActionMessage(undefined); }}>關閉</button>
      </div>}

      {data.status.running && <div className="cross-workbench-status" role="status">
        <span>背景工作進行中：{runningWorkbench === "meeting" ? "會議" : runningWorkbench === "lecture" ? "課堂" : "未知類型"} · {data.status.course || data.status.session || "未命名工作"} · {phaseLabels[phase || ""] || phase || "處理中"}
          {data.status.work_type === "meeting" && data.status.status?.diarization && Number.isFinite(data.status.status.diarization.processed_seconds)
            && <> · {data.status.status.diarization.stage === "segmentation" ? "發言者辨識" : data.status.status.diarization.stage === "retranscription" ? "重新轉錄" : "會後處理"} {formatDuration(data.status.status.diarization.processed_seconds)}
              {Number.isFinite(data.status.status.diarization.total_seconds) && (data.status.status.diarization.total_seconds ?? 0) > 0
                ? ` / ${formatDuration(data.status.status.diarization.total_seconds ?? undefined)}` : ""}</>}
        </span>
        {runningWorkbench && <button type="button" onClick={() => navigate(runningWorkbench === "meeting" ? { page: "meeting", target: "meeting-live" } : { page: "lecture", target: "live" })}>回到工作台</button>}
      </div>}

      <div hidden={page !== "meeting"} id="meeting-live">
        <MeetingWorkbench active={page === "meeting"} runtime={data.status} sessions={data.sessions} />
      </div>
      <div hidden={page !== "lecture"}>

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
              <button className={tab === "terms" ? "active" : ""} onClick={() => setTab("terms")}>術語候選 <small>{termData?.candidates.length ?? "…"}</small></button>
            </div>
            {visibleDetail && tab !== "terms" && <span className="updated">更新於 {formatDate(visibleDetail[tab].updated_at)}</span>}
          </div>
          <div className="document-pane">
            {tab === "terms" ? (termData ? <TermCandidates data={termData} onError={data.setError} onMessage={setActionMessage} onApplied={() => {
              if (data.selectedId) void api.termCandidates(data.selectedId).then(setTermData).catch(() => undefined);
            }} /> : <p>正在載入術語候選…</p>) :
              <MarkdownPane content={visibleDetail?.[tab].content} empty={tab === "transcript" ? t.emptyTranscript : t.emptyNotes} />}
          </div>
        </section>

        <aside className="history-card" id="history">
          <div className="card-title"><div><p className="eyebrow">LIBRARY</p><h2>{t.history}</h2></div><button title={t.refresh} onClick={() => void data.refreshSessions()}><Icon name="refresh" /></button></div>
          <div className="session-list">
            {data.loading && <p className="empty-list">正在載入…</p>}
            {!data.loading && lectureSessions.length === 0 && <p className="empty-list">還沒有課堂紀錄。</p>}
            {lectureSessions.map((session) => <button className={data.selectedId === session.id ? "session active" : "session"} onClick={() => data.setSelectedId(session.id)} key={session.id}>
              <span className="session-icon"><Icon name="book" /></span>
              <span className="session-copy"><strong>{session.course || session.id}</strong><small>{formatDate(session.started_at)} · {formatDuration(session.elapsed)}</small></span>
              <span className={`phase-dot ${session.phase || "unknown"}`} title={phaseLabels[session.phase || ""] || session.phase} />
            </button>)}
          </div>
          {visibleDetail && visibleDetail.session.has_transcript && <SummaryActions
            key={data.selectedId} sessionId={data.selectedId!} models={data.models}
            disabled={data.status.running} onChanged={data.refresh}
            onError={data.setError} onMessage={setActionMessage} />}
        </aside>
      </div>

      <div id="lecture-sharing"><SharePanel selectedId={data.selectedId} /></div>

      <CourseEditor
        courses={data.courses}
        onChanged={data.refresh}
        onError={data.setError}
        onMessage={setActionMessage}
      />
      </div>
      <div hidden={page !== "local-settings"}>
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
      </div>
    </main>
  </div>;
}
