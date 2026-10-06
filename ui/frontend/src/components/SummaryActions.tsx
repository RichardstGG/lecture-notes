import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { ModelInventory } from "../types";

interface Props {
  sessionId: string;
  models?: ModelInventory;
  disabled: boolean;
  onChanged: () => Promise<unknown>;
  onError: (message?: string) => void;
  onMessage: (message: string) => void;
}

export function SummaryActions({ sessionId, models, disabled, onChanged, onError, onMessage }: Props) {
  const [upstream, setUpstream] = useState("");
  const [mode, setMode] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    onError(undefined);
    try {
      const result = await api.summarize(sessionId, mode === "section" ? label : mode || undefined, upstream || undefined);
      onMessage(result.message);
      await onChanged();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法啟動總結");
    } finally {
      setBusy(false);
    }
  }
  return <form className="run-form summary-actions" onSubmit={submit}>
    <label>摘要上游<select aria-label="補做摘要上游" value={upstream} onChange={(event) => setUpstream(event.target.value)}>
      <option value="">使用課程／原工作階段設定</option>
      {(models?.summary_upstreams?.options || [{ id: "local", name: "本地 GPU" }]).map((item) =>
        <option key={item.id} value={item.id}>{item.name}</option>)}
    </select></label>
    <label>補做／重做<select aria-label="補做或重做" value={mode} onChange={(event) => setMode(event.target.value)}>
      <option value="">補做未處理段落</option>
      <option value="all">重做全部（備份舊筆記）</option>
      <option value="section">重做指定段落</option>
    </select></label>
    {mode === "section" && <label>段落時間<input aria-label="段落時間" value={label} onChange={(event) => setLabel(event.target.value)} placeholder="00:05:00" pattern="[0-9]{2}:[0-9]{2}:[0-9]{2}" required /></label>}
    <small>API 上游會接收逐字稿；失敗段落請選重做。</small>
    <button className="button secondary full" disabled={busy || disabled} type="submit">{busy ? "啟動中…" : "執行課堂摘要"}</button>
  </form>;
}
