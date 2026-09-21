import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Course, DeviceInventory, DoctorResult } from "../types";
import { Icon } from "./Icon";

interface Props {
  courses: Course[];
  devices?: DeviceInventory;
  devicesError?: string;
  devicesLoading: boolean;
  onDevicesChanged: (devices: DeviceInventory) => void;
  onRefreshDevices: () => Promise<void>;
  onError: (message?: string) => void;
  onMessage: (message?: string) => void;
}

export function LocalSettings({
  courses, devices, devicesError, devicesLoading, onDevicesChanged,
  onRefreshDevices, onError, onMessage,
}: Props) {
  const [source, setSource] = useState("default");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [doctorCourse, setDoctorCourse] = useState("");
  const [doctorMic, setDoctorMic] = useState(false);
  const [checking, setChecking] = useState(false);
  const [diagnostics, setDiagnostics] = useState<DoctorResult>();
  const validCourses = useMemo(() => courses.filter((course) => !course.error), [courses]);
  const sourceIds = useMemo(() => new Set(devices?.sources.map((item) => item.id)), [devices]);
  const configuredMissing = !!devices && devices.current !== "default"
    && !sourceIds.has(devices.current);

  useEffect(() => {
    if (devices) setSource(devices.current || "default");
  }, [devices]);

  async function saveSource() {
    setSaving(true);
    onError(undefined);
    onMessage(undefined);
    try {
      const updated = await api.selectDevice(source);
      onDevicesChanged(updated);
      onMessage("已將麥克風儲存到 config/local.toml");
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法儲存麥克風設定");
    } finally {
      setSaving(false);
    }
  }

  async function testSource() {
    setTesting(true);
    onError(undefined);
    onMessage(undefined);
    try {
      const result = await api.testDevice(source);
      onMessage(result.message);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "麥克風測試失敗");
    } finally {
      setTesting(false);
    }
  }

  async function runDoctor() {
    setChecking(true);
    onError(undefined);
    try {
      setDiagnostics(await api.doctor(doctorCourse || undefined, doctorMic));
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "環境檢查失敗");
    } finally {
      setChecking(false);
    }
  }

  return <section className="local-card" id="local-settings">
    <div className="local-card-header">
      <div>
        <p className="eyebrow">THIS COMPUTER</p>
        <h2>本機裝置與診斷</h2>
        <p>選擇麥克風並寫入不會進 Git 的 local.toml，或執行 lec doctor 檢查環境。</p>
      </div>
      <button className="icon-button" title="重新載入麥克風" disabled={devicesLoading} onClick={() => void onRefreshDevices()}>
        <Icon name="refresh" />
      </button>
    </div>

    <div className="local-grid">
      <section className="local-pane">
        <div className="local-pane-heading">
          <div><h3>麥克風</h3><p>測試會錄音 3 秒；儲存後成為本機預設。</p></div>
          {devices && <span>{devices.sources.length} 個裝置</span>}
        </div>
        {devicesError && <p className="local-inline-error">{devicesError}</p>}
        <label>
          <span>錄音來源</span>
          <select value={source} disabled={!devices || devicesLoading} onChange={(event) => setSource(event.target.value)}>
            <option value="default">系統預設{devices?.default ? ` → ${devices.default}` : ""}</option>
            {configuredMissing && <option value={devices.current}>{devices.current}（目前設定，未偵測到）</option>}
            {devices?.sources.map((item) => <option value={item.id} key={item.id}>
              {item.description || item.name}{item.state ? ` · ${item.state}` : ""}
            </option>)}
          </select>
        </label>
        {!devicesLoading && devices?.sources.length === 0 && <p className="local-hint">沒有偵測到麥克風；可先執行右側環境檢查查看原因。</p>}
        <div className="local-actions">
          <button className="button secondary" disabled={!devices || saving || testing} onClick={() => void testSource()}>
            {testing ? <><span className="button-spinner" aria-hidden="true" />錄音中…</> : "測試 3 秒"}
          </button>
          <button className="button primary" disabled={!devices || saving || testing || source === devices.current} onClick={() => void saveSource()}>
            {saving ? "儲存中…" : "儲存為本機預設"}
          </button>
        </div>
      </section>

      <section className="local-pane doctor-pane">
        <div className="local-pane-heading">
          <div><h3>環境檢查</h3><p>檢查工具、引擎、模型、port、資料夾與麥克風。</p></div>
          {diagnostics && <span className={diagnostics.summary.failures ? "has-failures" : ""}>
            {diagnostics.summary.failures} 錯誤 · {diagnostics.summary.warnings} 警告
          </span>}
        </div>
        <div className="doctor-controls">
          <label><span>課程設定（選填）</span><select value={doctorCourse} onChange={(event) => setDoctorCourse(event.target.value)}><option value="">只檢查本機預設</option>{validCourses.map((course) => <option value={course.id} key={course.id}>{course.name || course.id}</option>)}</select></label>
          <label className="check-field"><input type="checkbox" checked={doctorMic} onChange={(event) => setDoctorMic(event.target.checked)} /><span>同時錄音 3 秒</span></label>
          <button className="button secondary" disabled={checking} onClick={() => void runDoctor()}>{checking ? <><span className="button-spinner" aria-hidden="true" />檢查中…</> : "執行環境檢查"}</button>
        </div>
        {diagnostics ? <div className="doctor-results" aria-label="環境檢查結果">
          {diagnostics.items.map((item, index) => <div className="doctor-row" key={`${item.name}-${index}`}>
            <span className={`doctor-status ${item.status === "✖" ? "fail" : item.status === "⚠" ? "warn" : "ok"}`}>{item.status}</span>
            <strong>{item.name}</strong><p>{item.detail}</p>
          </div>)}
        </div> : <p className="local-hint">尚未執行檢查。若勾選錄音測試，系統可能會詢問麥克風權限。</p>}
      </section>
    </div>
  </section>;
}
