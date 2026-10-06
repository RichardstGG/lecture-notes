import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { SummaryUpstreamSetting, SummaryUpstreamWrite } from "../types";

interface Props {
  onChanged: () => Promise<unknown>;
  onError: (message?: string) => void;
  onMessage: (message?: string) => void;
}

type AuthMode = SummaryUpstreamWrite["auth_mode"];

const emptyForm = {
  id: "", name: "", base_url: "", model: "", auth_mode: "none" as AuthMode,
  api_key: "", api_key_env: "",
};

const authLabels: Record<AuthMode, string> = {
  none: "無認證", api_key: "API key 已設定", environment: "環境變數",
};

export function UpstreamSettings({ onChanged, onError, onMessage }: Props) {
  const [upstreams, setUpstreams] = useState<SummaryUpstreamSetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string>();
  const [editing, setEditing] = useState<string>();
  const [form, setForm] = useState(emptyForm);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setUpstreams((await api.summaryUpstreams()).upstreams);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法讀取摘要上游設定");
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { void refresh(); }, [refresh]);

  function startCreate() {
    setEditing(undefined);
    setForm(emptyForm);
    onError(undefined);
  }

  function startEdit(item: SummaryUpstreamSetting) {
    setEditing(item.id);
    setForm({ ...emptyForm, id: item.id, name: item.name, auth_mode: item.auth_mode });
    onError(undefined);
    onMessage("連線資料與認證不會從後端讀回；覆寫時請重新輸入完整設定。");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    onError(undefined);
    onMessage(undefined);
    const payload: SummaryUpstreamWrite = {
      ...form,
      id: editing ? undefined : form.id,
      api_key: form.auth_mode === "api_key" ? form.api_key : undefined,
      api_key_env: form.auth_mode === "environment" ? form.api_key_env : undefined,
    };
    try {
      if (editing) await api.updateSummaryUpstream(editing, payload);
      else await api.createSummaryUpstream(payload);
      setEditing(undefined);
      setForm(emptyForm);
      await Promise.all([refresh(), onChanged()]);
      onMessage(editing ? "已覆寫摘要 API 上游設定" : "已新增摘要 API 上游");
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法儲存摘要上游設定");
    } finally {
      setSaving(false);
    }
  }

  async function remove(item: SummaryUpstreamSetting) {
    if (!window.confirm(`刪除摘要上游「${item.name}」？使用此 ID 的課程需改選其他上游。`)) return;
    setDeleting(item.id);
    onError(undefined);
    onMessage(undefined);
    try {
      await api.deleteSummaryUpstream(item.id);
      if (editing === item.id) startCreate();
      await Promise.all([refresh(), onChanged()]);
      onMessage("已刪除摘要 API 上游");
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法刪除摘要上游設定");
    } finally {
      setDeleting(undefined);
    }
  }

  return <section className="upstream-settings" aria-label="摘要 API 上游設定">
    <div className="local-pane-heading">
      <div>
        <h3>摘要 API 上游</h3>
        <p>管理自架相容 API。連線資料與認證只寫入本機私有設定，不會由 UI API 讀回。</p>
      </div>
      <span>{upstreams.length} 個 API 上游</span>
    </div>

    <div className="upstream-settings-grid">
      <div className="upstream-list" aria-label="已儲存摘要上游">
        <div className="upstream-row builtin">
          <div><strong>本地 GPU</strong><small>local · 內建</small></div>
          <span>llama-server</span>
        </div>
        {loading && <p className="local-hint">正在載入摘要上游…</p>}
        {!loading && upstreams.length === 0 && <p className="local-hint">尚未儲存 API 上游。</p>}
        {upstreams.map((item) => <div className="upstream-row" key={item.id}>
          <div><strong>{item.name}</strong><small>{item.id} · {authLabels[item.auth_mode]}</small></div>
          <div className="upstream-row-actions">
            <button className="button secondary compact" type="button" onClick={() => startEdit(item)}>覆寫</button>
            <button className="button ghost-danger compact" type="button" disabled={deleting === item.id} onClick={() => void remove(item)}>
              {deleting === item.id ? "刪除中…" : "刪除"}
            </button>
          </div>
        </div>)}
      </div>

      <form className="upstream-form" onSubmit={submit}>
        <div className="upstream-form-heading">
          <strong>{editing ? `覆寫 ${editing}` : "新增 API 上游"}</strong>
          {editing && <button type="button" onClick={startCreate}>取消覆寫</button>}
        </div>
        <label><span>穩定 ID</span><input aria-label="上游 ID" required disabled={!!editing} pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}" value={form.id} onChange={(event) => setForm({ ...form, id: event.target.value })} placeholder="lab-gpu" /></label>
        <label><span>顯示名稱</span><input aria-label="上游顯示名稱" required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="內網 30B GPU" /></label>
        <label><span>Base URL</span><input aria-label="上游 Base URL" required type="url" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="http://192.0.2.10:8000/v1" /></label>
        <label><span>模型 ID</span><input aria-label="上游模型 ID" required value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="your-deployed-model-id" /></label>
        <label><span>認證方式</span><select aria-label="上游認證方式" value={form.auth_mode} onChange={(event) => setForm({ ...form, auth_mode: event.target.value as AuthMode, api_key: "", api_key_env: "" })}><option value="none">無認證</option><option value="environment">環境變數</option><option value="api_key">API key</option></select></label>
        {form.auth_mode === "api_key" && <label><span>API key</span><input aria-label="上游 API key" required type="password" autoComplete="new-password" value={form.api_key} onChange={(event) => setForm({ ...form, api_key: event.target.value })} /></label>}
        {form.auth_mode === "environment" && <label><span>環境變數名稱</span><input aria-label="上游認證環境變數" required pattern="[A-Za-z_][A-Za-z0-9_]{0,127}" value={form.api_key_env} onChange={(event) => setForm({ ...form, api_key_env: event.target.value })} placeholder="LEC_LAB_API_KEY" /></label>}
        <p className="local-hint">覆寫不會讀回舊 URL、模型或認證。儲存後 API key 欄位會立即清空。</p>
        <button className="button primary" disabled={saving} type="submit">{saving ? "儲存中…" : editing ? "覆寫上游" : "新增上游"}</button>
      </form>
    </div>
  </section>;
}
