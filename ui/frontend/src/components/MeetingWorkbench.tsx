import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { AudioUpload, DeviceInventory, Meeting, RuntimeStatus, SessionSummary } from "../types";
import { sessionIdFromPath } from "../utils";
import { SharePanel } from "./SharePanel";
import { MeetingSessionPanel } from "./MeetingSessionPanel";

interface Props {
  active?: boolean;
  runtime: RuntimeStatus;
  sessions?: SessionSummary[];
  devices?: DeviceInventory;
  onChanged?: () => Promise<unknown>;
}

export function MeetingWorkbench({ active = true, runtime, sessions = [], devices, onChanged }: Props) {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [meetingId, setMeetingId] = useState("");
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [speakers, setSpeakers] = useState("");
  const [mode, setMode] = useState<"live" | "file">("live");
  const [source, setSource] = useState("");
  const [uploaded, setUploaded] = useState<AudioUpload>();
  const [pending, setPending] = useState<string>();
  const [stopMode, setStopMode] = useState<"normal" | "force">();
  const [error, setError] = useState<string>();
  const [message, setMessage] = useState<string>();
  const [content, setContent] = useState<string>();
  const [selectedMeetingId, setSelectedMeetingId] = useState("");
  const meetingSessions = sessions.filter((session) => session.work_type === "meeting");
  const selectedMeeting = meetingSessions.find((session) => session.id === selectedMeetingId) || meetingSessions[0];
  const validMeetings = meetings.filter((meeting) => !meeting.error);
  const selectedConfig = meetings.find((meeting) => meeting.id === meetingId) || validMeetings[0];
  const validSpeakers = Number.isInteger(Number(speakers)) && Number(speakers) >= 1 && Number(speakers) <= 30;
  const meetingRunning = runtime.running && runtime.work_type === "meeting";
  const runningId = sessionIdFromPath(runtime.session, meetingSessions.map((session) => session.id));

  useEffect(() => {
    if (!active) return;
    let current = true;
    setLoading(true);
    api.meetings().then((value) => { if (current) { setMeetings(value); setError(undefined); } })
      .catch((reason) => { if (current) setError(reason instanceof Error ? reason.message : "無法讀取會議設定"); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [active]);

  useEffect(() => {
    if (meetingRunning && runningId && meetingSessions.some((session) => session.id === runningId)) setSelectedMeetingId(runningId);
  }, [meetingRunning, runningId, sessions]);

  useEffect(() => { setStopMode(undefined); }, [runtime.running, runtime.pid]);

  async function action(kind: string, run: () => Promise<unknown>) {
    setPending(kind); setError(undefined); setMessage(undefined);
    try { await run(); await onChanged?.(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "無法完成會議操作"); }
    finally { setPending(undefined); }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    await action("create", async () => {
      const result = await api.createMeeting(name.trim(), `[meeting]\nname = ${JSON.stringify(name.trim())}\n`);
      setMeetings(await api.meetings()); setMeetingId(result.id); setContent(undefined); setName("");
      setMessage("會議設定已建立。");
    });
  }

  async function start(event: FormEvent) {
    event.preventDefault();
    if (!selectedConfig || !validSpeakers || runtime.running || (mode === "file" && !uploaded)) return;
    await action("start", async () => {
      await api.startMeeting({ meeting_id: selectedConfig.id, speakers: Number(speakers),
        ...(mode === "file" ? { input_file: uploaded!.path } : source ? { source } : {}),
      });
      setMessage("會議工作已啟動，進度將持續更新。");
    });
  }

  async function stop(force: boolean) {
    await action("stop", async () => {
      const result = await api.stop(force); setStopMode(force ? "force" : "normal"); setMessage(result.message);
    });
  }

  return <div className="meeting-workbench">
    {error && <p className="notice error" role="alert">{error}</p>}
    {message && <p className="notice" role="status">{message}</p>}
    <section className="meeting-card" id="meeting-settings">
      <div className="meeting-card-heading"><div><p className="eyebrow">MEETING SETUP</p><h2>開始一場會議</h2></div></div>
      <p className="meeting-help">錄音或匯入後保留來源與原逐字稿；會後可另行辨識發言者。代號只在本場會議內有效。</p>
      <form onSubmit={create} className="meeting-fields">
        <label><span>新會議名稱</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：系統設計討論" required maxLength={100} /></label>
        <button className="button secondary" disabled={!!pending || !name.trim()} type="submit">建立會議設定</button>
      </form>
      <form onSubmit={start}>
        <div className="meeting-fields">
          <label><span>會議設定</span><select value={selectedConfig?.id || ""} required disabled={loading || !!pending}
            onChange={(event) => { setMeetingId(event.target.value); setContent(undefined); }}>
            {!validMeetings.length && <option value="">{loading ? "載入中…" : "請先建立會議設定"}</option>}
            {meetings.map((meeting) => <option key={meeting.id} value={meeting.id} disabled={!!meeting.error}>{meeting.name || meeting.id}{meeting.error ? "（設定有誤）" : ""}</option>)}
          </select></label>
          <label><span>預計參與人數（必填）</span><input type="number" min="1" max="30" step="1" required value={speakers} onChange={(event) => setSpeakers(event.target.value)} /></label>
          <label><span>輸入方式</span><select value={mode} onChange={(event) => setMode(event.target.value as "live" | "file")} disabled={!!pending}>
            <option value="live">現場錄音</option><option value="file">匯入音檔</option>
          </select></label>
          <label><span>錄音來源模式</span><select value="single" disabled><option value="single">單一來源</option><option value="dual" disabled>系統輸出＋麥克風（v1 不提供）</option></select></label>
          {mode === "live" ? <label><span>錄音裝置（選填）</span><select value={source} onChange={(event) => setSource(event.target.value)} disabled={!!pending}>
            <option value="">使用會議／本機設定</option>
            {devices?.sources.map((device) => <option key={device.id} value={device.id}>{device.description || device.name}</option>)}
          </select></label> : <label><span>會議音檔</span><input type="file" accept="audio/*,video/*,.ogg,.wav,.flac,.m4a,.mp3,.webm" disabled={!!pending || runtime.running}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) { setUploaded(undefined); void action("upload", async () => { setUploaded(await api.uploadAudio(file)); }); }
              event.target.value = "";
            }} /></label>}
        </div>
        {mode === "file" && <p className="meeting-help">{pending === "upload" ? "正在準備音檔…" : uploaded ? `已準備：${uploaded.name}` : "請先選擇要匯入的音檔。"}</p>}
        <button className="button primary" type="submit" disabled={loading || !!pending || runtime.running || !selectedConfig || !!selectedConfig.error || !validSpeakers || (mode === "file" && !uploaded)}>
          {pending === "start" ? "啟動中…" : mode === "file" ? "開始匯入" : "開始錄音"}
        </button>
      </form>
      {runtime.running && <p className="meeting-help">目前已有工作進行中；兩個工作台共用單一工作限制。</p>}
      {selectedConfig && <details>
        <summary>編輯會議設定</summary>
        <button className="button secondary" disabled={!!pending} onClick={() => void action("load-config", async () => { setContent((await api.meeting(selectedConfig.id)).content); })}>載入設定內容</button>
        {content !== undefined && <form onSubmit={(event) => {
          event.preventDefault();
          void action("save-config", async () => {
            await api.updateMeeting(selectedConfig.id, content); setMeetings(await api.meetings()); setMessage("會議設定已儲存。");
          });
        }}>
          <label><span>會議 TOML 設定</span><textarea rows={10} value={content} onChange={(event) => setContent(event.target.value)} /></label>
          <button type="submit" className="button secondary" disabled={!!pending}>儲存會議設定</button>
        </form>}
      </details>}
    </section>
    <MeetingSessionPanel active={active} runtime={runtime} sessions={meetingSessions} selectedId={selectedMeeting?.id} onSelect={setSelectedMeetingId}
      pending={!!pending} stopMode={stopMode} onStop={(force) => void stop(force)}
      onDiarize={(id, count) => void action("diarize", async () => { await api.diarize(id, count); setMessage("會後辨識已啟動。"); })} />
    <section className="meeting-card" id="meeting-sharing">
      <div className="meeting-card-heading"><div><p className="eyebrow">LAN SHARING</p><h2>分享會議逐字稿</h2></div></div>
      <p className="meeting-help">分享選定場次的原逐字稿與帶發言者逐字稿。</p>
      <label className="meeting-share-selection"><span>分享場次</span><select value={selectedMeeting?.id || ""} onChange={(event) => setSelectedMeetingId(event.target.value)} disabled={!meetingSessions.length}>
        {!meetingSessions.length && <option value="">目前沒有可分享的會議場次</option>}
        {meetingSessions.map((session) => <option key={session.id} value={session.id}>{session.course || session.id}</option>)}
      </select></label>
      <SharePanel selectedId={selectedMeeting?.id} workType="meeting" />
    </section>
  </div>;
}
