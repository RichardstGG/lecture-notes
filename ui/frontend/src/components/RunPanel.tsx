import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api } from "../api";
import type { Course, RuntimeStatus } from "../types";
import { Icon } from "./Icon";

interface Props {
  courses: Course[];
  status: RuntimeStatus;
  onChanged: () => Promise<unknown>;
  onError: (message?: string) => void;
}

export function RunPanel({ courses, status, onChanged, onError }: Props) {
  const validCourses = useMemo(() => courses.filter((course) => !course.error), [courses]);
  const [course, setCourse] = useState("");
  const [mode, setMode] = useState<"live" | "file">("live");
  const [inputFile, setInputFile] = useState("");
  const [model, setModel] = useState("");
  const [starting, setStarting] = useState(false);
  const [stopMode, setStopMode] = useState<"normal" | "force">();
  const [stopRequesting, setStopRequesting] = useState(false);

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
      });
      await onChanged();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法開始課程");
    } finally {
      setStarting(false);
    }
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
    {mode === "file" && <label className="wide-field">
      <span>本機音檔路徑</span>
      <input value={inputFile} onChange={(event) => setInputFile(event.target.value)} placeholder="/home/you/lecture.ogg" required />
    </label>}
    <label>
      <span>模型覆寫（選填）</span>
      <input list="known-models" value={model} onChange={(event) => setModel(event.target.value)} placeholder="使用課程預設" />
      <datalist id="known-models">
        {[...new Set(validCourses.map((item) => item.model).filter(Boolean))].map((name) => <option value={name} key={name} />)}
      </datalist>
    </label>
    <button className="button primary" disabled={starting || !course} type="submit">{starting ? "處理中…" : "開始處理"}</button>
  </form>;
}
