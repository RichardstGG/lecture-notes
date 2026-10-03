import { useState } from "react";
import type { RuntimeStatus, SessionSummary } from "../types";
import { formatDuration } from "../utils";
import { SharePanel } from "./SharePanel";

type PreviewStage = "ready" | "recording" | "diarizing" | "done";

const previewSpeakers = [
  { id: "S01", seconds: 1720, share: "51.7%", text: "我們先確認今天的系統架構。" },
  { id: "S02", seconds: 606, share: "18.2%", text: "API 的回傳格式要保留相容性。" },
  { id: "S03", seconds: 593, share: "17.8%", text: "部署前先跑一次 smoke test。" },
  { id: "S04", seconds: 319, share: "9.6%", text: "我會再檢查英文術語。" },
  { id: "S05", seconds: 39, share: "1.2%", text: "收到。" },
  { id: "S06", seconds: 27, share: "0.8%", text: "好。" },
  { id: "S07", seconds: 13, share: "0.4%", text: "可以。" },
  { id: "S08", seconds: 8, share: "0.2%", text: "嗯。" },
  { id: "S09", seconds: 3, share: "0.1%", text: "對。" },
  { id: "S10", seconds: 1, share: "0.0%", text: "好。" },
];

interface Props {
  runtime: RuntimeStatus;
  sessions?: SessionSummary[];
}

export function MeetingWorkbench({ runtime, sessions = [] }: Props) {
  const [name, setName] = useState("");
  const [speakers, setSpeakers] = useState(10);
  const [stage, setStage] = useState<PreviewStage>("ready");
  const [showShort, setShowShort] = useState(false);
  const [resultTab, setResultTab] = useState<"raw" | "speakers">("speakers");
  const [selectedMeetingId, setSelectedMeetingId] = useState("");
  const meetingSessions = sessions.filter((session) => session.work_type === "meeting");
  const selectedMeeting = meetingSessions.find((session) => session.id === selectedMeetingId) || meetingSessions[0];
  const prominent = previewSpeakers.filter((speaker) => speaker.seconds >= 30 && Number.parseFloat(speaker.share) >= 2);
  const short = previewSpeakers.filter((speaker) => speaker.seconds < 30 || Number.parseFloat(speaker.share) < 2);
  const isBusy = runtime.running;

  return <div className="meeting-workbench">
    <div className="meeting-preview-note" role="status">
      <strong>會議工作台預覽</strong>
      <span>此頁使用示範資料，尚未連接會議 CLI 或發言者辨識。下方示範控制不會啟動或停止真正的工作。</span>
    </div>

    <section className="meeting-card">
      <div className="meeting-card-heading">
        <div><p className="eyebrow">MEETING SETUP</p><h2>開始一場會議</h2></div>
        <span className="meeting-demo-tag">介面預覽</span>
      </div>
      <p className="meeting-help">會議流程將沿用本機錄音、音檔匯入與逐字稿。發言者代號在會後產生，只在本場會議內有效。</p>
      <div className="meeting-fields">
        <label><span>會議名稱</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：系統設計討論" /></label>
        <label><span>預計參與人數（必填）</span><input type="number" min="1" max="30" required value={speakers} onChange={(event) => setSpeakers(Number(event.target.value))} /></label>
        <label><span>來源</span><select disabled><option>現場錄音 / 匯入音檔（待串接）</option></select></label>
      </div>
      <button className="button primary" disabled title="會議 CLI 尚未實作">開始錄音（待串接）</button>
      {isBusy && <p className="meeting-help">目前已有工作進行中；兩個工作台共用單一工作限制。</p>}
    </section>

    <section className="meeting-card">
      <div className="meeting-card-heading"><div><p className="eyebrow">WORKFLOW PREVIEW</p><h2>會後辨識進度</h2></div></div>
      <div className="meeting-demo-controls" role="group" aria-label="示範狀態">
        {(["ready", "recording", "diarizing", "done"] as const).map((value) =>
          <button type="button" className={stage === value ? "active" : ""} onClick={() => setStage(value)} key={value}>
            {{ ready: "待開始", recording: "錄音中", diarizing: "辨識中", done: "已完成" }[value]}
          </button>)}
      </div>
      {stage === "ready" && <p className="meeting-help">錄音完成後保留原始音檔與逐字稿，再以預計人數啟動會後辨識。</p>}
      {stage === "recording" && <div className="meeting-progress"><strong>示範：錄音中</strong><p>原逐字稿持續更新；切換工作台不會停止背景工作。</p></div>}
      {stage === "diarizing" && <div className="meeting-progress" role="progressbar" aria-label="示範辨識進度" aria-valuenow={47} aria-valuemin={0} aria-valuemax={100}>
        <strong>重新轉錄 · 已處理 42:00 / 88:00</strong>
        <div className="meeting-progress-track"><span style={{ width: "47%" }} /></div>
        <p>要求 {speakers || 10} 位 · 已找到 4 位主要發言者 · 其他短發言 6 位</p>
        <p>實際可用代號數可能少於或多於主要發言者數。</p>
      </div>}
      {stage === "done" && <p className="meeting-help">示範完成。正式版本將提供取消、強制停止及從保留來源重新辨識；此預覽不操作背景工作。</p>}
    </section>

    <section className="meeting-card">
      <div className="meeting-card-heading"><div><p className="eyebrow">RESULT PREVIEW</p><h2>會議結果與歷史</h2></div><span className="meeting-demo-tag">示範會議 · 10 人</span></div>
      <div className="meeting-demo-controls" role="tablist" aria-label="示範逐字稿">
        <button role="tab" aria-selected={resultTab === "raw"} className={resultTab === "raw" ? "active" : ""} onClick={() => setResultTab("raw")}>原逐字稿</button>
        <button role="tab" aria-selected={resultTab === "speakers"} className={resultTab === "speakers" ? "active" : ""} onClick={() => setResultTab("speakers")}>帶發言者逐字稿</button>
      </div>
      {resultTab === "raw" ? <div className="meeting-transcript"><p>[00:01:12] 我們先確認今天的系統架構。API 的回傳格式要保留相容性。</p></div> : <>
        <div className="meeting-speaker-list">
          {prominent.map((speaker) => <div key={speaker.id}><strong>{speaker.id}</strong><span>{formatDuration(speaker.seconds)} · {speaker.share}</span></div>)}
        </div>
        <button className="meeting-short-toggle" type="button" aria-expanded={showShort} onClick={() => setShowShort(!showShort)}>
          其他短發言 · {short.length} 位 {showShort ? "收起" : "展開"}
        </button>
        {showShort && <div className="meeting-speaker-list short">{short.map((speaker) => <div key={speaker.id}><strong>{speaker.id}</strong><span>{formatDuration(speaker.seconds)} · {speaker.share}</span></div>)}</div>}
        <div className="meeting-transcript">{previewSpeakers.slice(0, 4).map((speaker, index) =>
          <p key={speaker.id}><time>[00:0{index + 1}:12.000–00:0{index + 1}:18.000]</time> <strong>{speaker.id}:</strong> {speaker.text}</p>)}</div>
      </>}
    </section>

    <section className="meeting-card">
      <div className="meeting-card-heading"><div><p className="eyebrow">LAN SHARING</p><h2>分享會議逐字稿</h2></div></div>
      <p className="meeting-help">選擇真實會議場次後，可用現有內網唯讀分享提供原逐字稿與帶發言者逐字稿。示範資料不會對外分享。</p>
      <label className="meeting-share-selection"><span>分享場次</span><select value={selectedMeeting?.id || ""} onChange={(event) => setSelectedMeetingId(event.target.value)} disabled={!meetingSessions.length}>
        {!meetingSessions.length && <option value="">目前沒有可分享的會議場次</option>}
        {meetingSessions.map((session) => <option key={session.id} value={session.id}>{session.course || session.id}</option>)}
      </select></label>
      <SharePanel selectedId={selectedMeeting?.id} workType="meeting" />
    </section>
  </div>;
}
