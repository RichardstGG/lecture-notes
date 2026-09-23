import { useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from "react";
import { api } from "../api";
import type { AudioUpload, Course, DeviceInventory, ModelInventory, RuntimeStatus } from "../types";
import { Icon } from "./Icon";

interface Props {
  courses: Course[];
  devices?: DeviceInventory;
  models?: ModelInventory;
  status: RuntimeStatus;
  onChanged: () => Promise<unknown>;
  onError: (message?: string) => void;
}

function formatModelSize(bytes?: number): string | undefined {
  if (bytes === undefined) return undefined;
  return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024 ** 2) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 ** 2)).toFixed(bytes < 10 * 1024 ** 2 ? 1 : 0)} MB`;
}

export function RunPanel({ courses, devices, models, status, onChanged, onError }: Props) {
  const validCourses = useMemo(() => courses.filter((course) => !course.error), [courses]);
  const [course, setCourse] = useState("");
  const [mode, setMode] = useState<"live" | "file">("live");
  const [inputFile, setInputFile] = useState("");
  const [inputMethod, setInputMethod] = useState<"picker" | "path">("picker");
  const [uploadedFile, setUploadedFile] = useState<AudioUpload>();
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [model, setModel] = useState("");
  const [source, setSource] = useState("");
  const [transcribeOnly, setTranscribeOnly] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stopMode, setStopMode] = useState<"normal" | "force">();
  const [stopRequesting, setStopRequesting] = useState(false);
  const selectedCourse = validCourses.find((item) => item.id === course);
  const defaultModel = selectedCourse?.model || models?.summary.selected;
  const defaultModelInfo = models?.summary.models.find((item) => item.id === defaultModel);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!course && validCourses[0]) setCourse(validCourses[0].id);
  }, [course, validCourses]);

  useEffect(() => {
    if (!status.running) setStopMode(undefined);
  }, [status.running]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!course || (mode === "file" && !inputFile.trim())) return;
    setStarting(true);
    onError(undefined);
    try {
      await api.start({
        course,
        input_file: mode === "file" ? inputFile.trim() : undefined,
        model: model || undefined,
        ...(mode === "live" && source ? { source } : {}),
        overrides: transcribeOnly ? { "summary.enabled": false } : undefined,
      });
      await onChanged();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法開始課程");
    } finally {
      setStarting(false);
    }
  }

  async function prepareFile(file?: File) {
    if (!file) return;
    setUploading(true);
    onError(undefined);
    try {
      const uploaded = await api.uploadAudio(file);
      setUploadedFile(uploaded);
      setInputFile(uploaded.path);
    } catch (reason) {
      setUploadedFile(undefined);
      setInputFile("");
      onError(reason instanceof Error ? reason.message : "無法準備音檔");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  function switchInputMethod(method: "picker" | "path") {
    setInputMethod(method);
    setInputFile(method === "picker" ? uploadedFile?.path ?? "" : "");
    onError(undefined);
  }

  function dropFile(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (uploading) return;
    void prepareFile(event.dataTransfer.files[0]);
  }

  async function stop(force: boolean) {
    setStopMode(force ? "force" : "normal");
    setStopRequesting(true);
    onError(undefined);
    try {
      await api.stop(force);
      await onChanged();
    } catch (reason) {
      setStopMode(undefined);
      onError(reason instanceof Error ? reason.message : "無法停止課程");
    } finally {
      setStopRequesting(false);
    }
  }

  if (status.running) {
    return <div className="run-actions">
      <button className="button danger" aria-busy={stopMode === "normal"} disabled={stopRequesting || stopMode !== undefined} onClick={() => void stop(false)}>
        {stopMode === "normal" ? <span className="button-spinner" aria-hidden="true" /> : <Icon name="stop" />}
        {stopMode === "normal" ? "正在停止…" : "正常停止"}
      </button>
      <button className="button ghost-danger" aria-busy={stopMode === "force"} disabled={stopRequesting || stopMode === "force"} onClick={() => void stop(true)}>
        {stopMode === "force" && <span className="button-spinner" aria-hidden="true" />}
        {stopMode === "force" ? "正在中止…" : "立即停止"}
      </button>
      <p>正常停止會完成目前片段後收尾；立即停止只在必要時使用。</p>
    </div>;
  }

  return <form className="run-form" onSubmit={submit}>
    <label>
      <span>課程</span>
      <select value={course} onChange={(event) => setCourse(event.target.value)} required>
        {validCourses.length === 0 && <option value="">尚無可用課程</option>}
        {validCourses.map((item) => <option value={item.id} key={item.id}>{item.name || item.id}</option>)}
      </select>
    </label>
    <div className="mode-switch" role="group" aria-label="輸入方式">
      <button type="button" className={mode === "live" ? "active" : ""} onClick={() => setMode("live")}><Icon name="mic" />現場錄音</button>
      <button type="button" className={mode === "file" ? "active" : ""} onClick={() => setMode("file")}><Icon name="file" />處理音檔</button>
    </div>
    {mode === "file" && <div className="wide-field file-field">
      <div className="file-field-heading">
        <span>本機音檔</span>
        <button type="button" disabled={uploading} onClick={() => switchInputMethod(inputMethod === "picker" ? "path" : "picker")}>
          {inputMethod === "picker" ? "手動輸入路徑" : "改用檔案選擇器"}
        </button>
      </div>
      {inputMethod === "picker" ? <>
        <input
          ref={fileInput}
          className="sr-only"
          aria-label="選擇音檔"
          type="file"
          accept="audio/*,video/*,.3gp,.aac,.aif,.aiff,.amr,.caf,.flac,.m4a,.mka,.mkv,.mov,.mp3,.mp4,.mpeg,.mpg,.mts,.oga,.ogg,.opus,.ts,.wav,.webm,.wma"
          onChange={(event) => void prepareFile(event.target.files?.[0])}
        />
        <div
          className={`file-picker${dragging ? " dragging" : ""}${uploadedFile ? " ready" : ""}`}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={dropFile}
        >
          <Icon name="file" />
          <div>
            <strong>{uploading ? "正在準備音檔…" : uploadedFile?.name ?? "拖放音檔，或從電腦選擇"}</strong>
            <small>{uploadedFile
              ? `${formatFileSize(uploadedFile.size_bytes)} · 已準備在本機處理`
              : "檔案只會複製到這台電腦的暫存區"}</small>
          </div>
          <button className="button secondary compact" type="button" disabled={uploading} onClick={() => fileInput.current?.click()}>
            {uploadedFile ? "重新選擇" : "選擇檔案"}
          </button>
        </div>
      </> : <label>
        <span className="sr-only">本機音檔路徑</span>
        <input aria-label="本機音檔路徑" value={inputFile} onChange={(event) => setInputFile(event.target.value)} placeholder="/home/you/lecture.ogg" required />
      </label>}
    </div>}
    {mode === "live" && <label>
      <span>麥克風（選填）</span>
      <select value={source} onChange={(event) => setSource(event.target.value)} disabled={!devices}>
        <option value="">{devices
          ? `使用本機設定（${devices.current === "default"
            ? `系統預設${devices.default ? ` → ${devices.default}` : ""}`
            : devices.current}）`
          : "正在載入麥克風…"}</option>
        {devices?.sources.map((item) => <option value={item.id} key={item.id}>
          {item.description || item.name}{item.state ? ` · ${item.state}` : ""}
        </option>)}
      </select>
    </label>}
    <label>
      <span>總結模型（選填）</span>
      <select value={model} onChange={(event) => setModel(event.target.value)} disabled={!models}>
        <option value="">{models
          ? `使用課程預設${defaultModel ? `（${defaultModel}${defaultModelInfo && !defaultModelInfo.available ? "，未安裝" : ""}）` : ""}`
          : "正在載入模型清單…"}</option>
        {models?.summary.models.map((item) => <option value={item.id} disabled={!item.available} key={item.id}>
          {item.id}{item.available
            ? formatModelSize(item.size_bytes) ? ` · ${formatModelSize(item.size_bytes)}` : ""
            : "（未安裝）"}
        </option>)}
      </select>
    </label>
    <label className="check-field">
      <input type="checkbox" checked={transcribeOnly} onChange={(event) => setTranscribeOnly(event.target.checked)} />
      <span>只轉錄（不啟動總結模型）</span>
    </label>
    <button className="button primary" disabled={starting || uploading || !course || (mode === "file" && !inputFile.trim())} type="submit">{starting ? "處理中…" : "開始處理"}</button>
  </form>;
}
