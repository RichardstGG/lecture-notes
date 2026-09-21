import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { Course, CourseDetail } from "../types";

interface Props {
  courses: Course[];
  onChanged: () => Promise<unknown>;
  onError: (message?: string) => void;
  onMessage: (message?: string) => void;
}

export function CourseEditor({ courses, onChanged, onError, onMessage }: Props) {
  const [selectedId, setSelectedId] = useState<string>();
  const [detail, setDetail] = useState<CourseDetail>();
  const [content, setContent] = useState("");
  const [newCourse, setNewCourse] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const dirty = detail !== undefined && content !== detail.content;

  useEffect(() => {
    if (!selectedId && courses[0]) setSelectedId(courses[0].id);
  }, [courses, selectedId]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(undefined);
      setContent("");
      return;
    }
    let active = true;
    setDetail(undefined);
    setContent("");
    setLoading(true);
    void api.course(selectedId).then((value) => {
      if (!active) return;
      setDetail(value);
      setContent(value.content);
      onError(undefined);
    }).catch((reason) => {
      if (active) onError(reason instanceof Error ? reason.message : "無法讀取課程設定");
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [selectedId, onError]);

  useEffect(() => {
    if (!dirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  function selectCourse(courseId: string) {
    if (dirty && !window.confirm("尚未儲存的修改會遺失，仍要切換課程嗎？")) return;
    setSelectedId(courseId);
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    if (dirty && !window.confirm("尚未儲存的修改會遺失，仍要建立新課程嗎？")) return;
    const courseId = newCourse.trim();
    if (!courseId) return;
    setSaving(true);
    onError(undefined);
    onMessage(undefined);
    try {
      const created = await api.createCourse(courseId);
      setSelectedId(created.id);
      setDetail(created);
      setContent(created.content);
      setNewCourse("");
      await onChanged();
      onMessage(`已建立課程「${created.id}」`);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法建立課程");
    } finally {
      setSaving(false);
    }
  }

  async function save() {
    if (!detail || !dirty) return;
    setSaving(true);
    onError(undefined);
    onMessage(undefined);
    try {
      const updated = await api.updateCourse(detail.id, content);
      setDetail(updated);
      setContent(updated.content);
      await onChanged();
      onMessage(`已儲存課程「${updated.id}」`);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法儲存課程設定");
    } finally {
      setSaving(false);
    }
  }

  return <section className="course-card" id="courses">
    <div className="course-card-header">
      <div>
        <p className="eyebrow">COURSES</p>
        <h2>課程設定</h2>
        <p>建立課程或直接編輯 TOML；儲存前會驗證完整合併設定。</p>
      </div>
      <form className="course-create" onSubmit={create}>
        <label htmlFor="new-course">新課程名稱</label>
        <div><input id="new-course" value={newCourse} onChange={(event) => setNewCourse(event.target.value)} placeholder="例如：資料結構" maxLength={100} /><button className="button primary" disabled={saving || !newCourse.trim()} type="submit">建立課程</button></div>
      </form>
    </div>

    <div className="course-editor-grid">
      <div className="course-list" aria-label="課程清單">
        {courses.length === 0 && <p>尚無課程，請先建立一門課。</p>}
        {courses.map((course) => <button className={selectedId === course.id ? "active" : ""} type="button" onClick={() => selectCourse(course.id)} key={course.id}>
          <span><strong>{course.name || course.id}</strong><small>{course.id}</small></span>
          {course.error && <i title={course.error}>需修正</i>}
        </button>)}
      </div>

      <div className="course-source">
        <div className="course-source-toolbar">
          <div><strong>{detail?.id || "選擇課程"}</strong>{detail && <small>{detail.file}</small>}</div>
          {dirty && <span>尚未儲存</span>}
        </div>
        {loading ? <p className="course-editor-empty">正在載入設定…</p> : detail ? <>
          <label className="sr-only" htmlFor="course-toml">課程 TOML</label>
          <textarea id="course-toml" value={content} onChange={(event) => setContent(event.target.value)} spellCheck={false} />
          <div className="course-editor-actions">
            <button className="button secondary" type="button" disabled={saving || !dirty} onClick={() => setContent(detail.content)}>還原</button>
            <button className="button primary" type="button" disabled={saving || !dirty} onClick={() => void save()}>{saving ? "驗證中…" : "儲存設定"}</button>
          </div>
        </> : <p className="course-editor-empty">從左側選擇課程，或建立新課程。</p>}
      </div>
    </div>
  </section>;
}
