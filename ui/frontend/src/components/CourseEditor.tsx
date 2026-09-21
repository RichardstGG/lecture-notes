import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { Course, CourseDetail, CourseVocabulary, GlossaryEntry } from "../types";

interface Props {
  courses: Course[];
  onChanged: () => Promise<unknown>;
  onError: (message?: string) => void;
  onMessage: (message?: string) => void;
}

interface EditableGlossary {
  term: string;
  means: string;
  aliases: string;
}

type EditorTab = "vocabulary" | "source";

function rowsFromVocabulary(vocabulary?: CourseVocabulary | null): EditableGlossary[] {
  return (vocabulary?.glossary ?? []).map((entry) => ({
    term: entry.term,
    means: entry.means,
    aliases: entry.aka.join("\n"),
  }));
}

function uniqueLines(value: string): string[] {
  const values = value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  return [...new Set(values)];
}

function rowsSnapshot(rows: EditableGlossary[]): string {
  return JSON.stringify(rows);
}

export function CourseEditor({ courses, onChanged, onError, onMessage }: Props) {
  const [selectedId, setSelectedId] = useState<string>();
  const [detail, setDetail] = useState<CourseDetail>();
  const [content, setContent] = useState("");
  const [termsText, setTermsText] = useState("");
  const [glossary, setGlossary] = useState<EditableGlossary[]>([]);
  const [tab, setTab] = useState<EditorTab>("vocabulary");
  const [newCourse, setNewCourse] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const sourceDirty = detail !== undefined && content !== detail.content;
  const vocabularyAvailable = detail?.vocabulary !== undefined && detail.vocabulary !== null;
  const vocabularyDirty = vocabularyAvailable && (
    termsText !== detail.vocabulary!.terms.join("\n")
    || rowsSnapshot(glossary) !== rowsSnapshot(rowsFromVocabulary(detail.vocabulary))
  );
  const dirty = sourceDirty || vocabularyDirty;
  const glossaryTerms = glossary.map((entry) => entry.term.trim()).filter(Boolean);
  const duplicateGlossaryTerm = glossaryTerms.find(
    (term, index) => glossaryTerms.indexOf(term) !== index,
  );
  const invalidGlossary = glossary.some((entry) => !entry.term.trim()) || !!duplicateGlossaryTerm;

  function applyDetail(value?: CourseDetail) {
    setDetail(value);
    setContent(value?.content ?? "");
    setTermsText(value?.vocabulary?.terms.join("\n") ?? "");
    setGlossary(rowsFromVocabulary(value?.vocabulary));
  }

  function restoreVocabulary() {
    setTermsText(detail?.vocabulary?.terms.join("\n") ?? "");
    setGlossary(rowsFromVocabulary(detail?.vocabulary));
  }

  useEffect(() => {
    if (!selectedId && courses[0]) setSelectedId(courses[0].id);
  }, [courses, selectedId]);

  useEffect(() => {
    if (!selectedId) {
      applyDetail(undefined);
      return;
    }
    let active = true;
    applyDetail(undefined);
    setLoading(true);
    void api.course(selectedId).then((value) => {
      if (!active) return;
      applyDetail(value);
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

  function selectTab(next: EditorTab) {
    if (next === tab) return;
    const currentDirty = tab === "source" ? sourceDirty : vocabularyDirty;
    if (currentDirty && !window.confirm("切換編輯方式會放棄尚未儲存的修改，是否繼續？")) return;
    if (tab === "source") setContent(detail?.content ?? "");
    else restoreVocabulary();
    setTab(next);
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
      applyDetail(created);
      setNewCourse("");
      await onChanged();
      onMessage(`已建立課程「${created.id}」`);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法建立課程");
    } finally {
      setSaving(false);
    }
  }

  async function saveSource() {
    if (!detail || !sourceDirty) return;
    setSaving(true);
    onError(undefined);
    onMessage(undefined);
    try {
      const updated = await api.updateCourse(detail.id, content);
      applyDetail(updated);
      await onChanged();
      onMessage(`已儲存課程「${updated.id}」`);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法儲存課程設定");
    } finally {
      setSaving(false);
    }
  }

  async function saveVocabulary() {
    if (!detail || !vocabularyDirty || invalidGlossary) return;
    const vocabulary: CourseVocabulary = {
      terms: uniqueLines(termsText),
      glossary: glossary.map<GlossaryEntry>((entry) => ({
        term: entry.term.trim(),
        means: entry.means.trim(),
        aka: uniqueLines(entry.aliases).filter((alias) => alias !== entry.term.trim()),
      })),
    };
    setSaving(true);
    onError(undefined);
    onMessage(undefined);
    try {
      const updated = await api.updateCourseVocabulary(detail.id, vocabulary);
      applyDetail(updated);
      await onChanged();
      onMessage(`已儲存課程「${updated.id}」的術語設定`);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法儲存術語設定");
    } finally {
      setSaving(false);
    }
  }

  function updateGlossary(index: number, key: keyof EditableGlossary, value: string) {
    setGlossary((current) => current.map((entry, position) => (
      position === index ? { ...entry, [key]: value } : entry
    )));
  }

  return <section className="course-card" id="courses">
    <div className="course-card-header">
      <div>
        <p className="eyebrow">COURSES</p>
        <h2>課程設定</h2>
        <p>管理辨識術語與錯字對照，或直接編輯 TOML；儲存前會驗證完整設定。</p>
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
          <div className="course-editor-tabs" role="tablist" aria-label="課程設定編輯方式">
            <button role="tab" aria-selected={tab === "vocabulary"} className={tab === "vocabulary" ? "active" : ""} type="button" onClick={() => selectTab("vocabulary")}>術語與錯字對照</button>
            <button role="tab" aria-selected={tab === "source"} className={tab === "source" ? "active" : ""} type="button" onClick={() => selectTab("source")}>原始 TOML</button>
          </div>

          {tab === "vocabulary" ? vocabularyAvailable ? <div className="vocabulary-editor" role="tabpanel">
            <section className="vocabulary-section">
              <div className="vocabulary-heading">
                <div><h3>Whisper 常用術語</h3><p>每行一個，建議控制在 20 個以內；同時提供給總結作為錯字參考。</p></div>
                <span>{uniqueLines(termsText).length} 個</span>
              </div>
              <label className="sr-only" htmlFor="course-terms">Whisper 常用術語（每行一個）</label>
              <textarea className="terms-editor" id="course-terms" value={termsText} onChange={(event) => setTermsText(event.target.value)} placeholder={"UNIX\nPOSIX\ninode"} spellCheck={false} />
            </section>

            <section className="vocabulary-section glossary-section">
              <div className="vocabulary-heading">
                <div><h3>總結錯字對照表</h3><p>正式術語可搭配辨識說明，以及逐字稿可能出現的錯誤寫法。</p></div>
                <button className="button secondary compact" type="button" onClick={() => setGlossary((current) => [...current, { term: "", means: "", aliases: "" }])}>新增對照</button>
              </div>
              {glossary.length === 0 && <p className="glossary-empty">尚無錯字對照；不需要時可保持空白。</p>}
              <div className="glossary-list">
                {glossary.map((entry, index) => <article className="glossary-row" key={index}>
                  <label><span>正式術語</span><input value={entry.term} onChange={(event) => updateGlossary(index, "term", event.target.value)} placeholder="Multics" /></label>
                  <label><span>辨識說明</span><input value={entry.means} onChange={(event) => updateGlossary(index, "means", event.target.value)} placeholder="貝爾實驗室的分時系統專案" /></label>
                  <label><span>常見錯字（每行一個）</span><textarea value={entry.aliases} onChange={(event) => updateGlossary(index, "aliases", event.target.value)} placeholder={"MUTIX\nMultix"} /></label>
                  <button className="glossary-remove" type="button" aria-label={`刪除對照 ${entry.term || index + 1}`} onClick={() => setGlossary((current) => current.filter((_item, position) => position !== index))}>移除</button>
                </article>)}
              </div>
              {invalidGlossary && <p className="field-error">{duplicateGlossaryTerm ? `正式術語「${duplicateGlossaryTerm}」重複` : "每一筆錯字對照都需要正式術語"}</p>}
            </section>
            <div className="course-editor-actions">
              <button className="button secondary" type="button" disabled={saving || !vocabularyDirty} onClick={restoreVocabulary}>還原</button>
              <button className="button primary" type="button" disabled={saving || !vocabularyDirty || invalidGlossary} onClick={() => void saveVocabulary()}>{saving ? "驗證中…" : "儲存術語設定"}</button>
            </div>
          </div> : <div className="vocabulary-unavailable" role="tabpanel">
            <strong>目前無法用表單讀取術語設定</strong>
            <p>請先切換到原始 TOML，修正 `whisper.terms` 或 `[summary.glossary]` 的格式。</p>
            <button className="button secondary" type="button" onClick={() => selectTab("source")}>開啟原始 TOML</button>
          </div> : <div className="source-editor" role="tabpanel">
            <label className="sr-only" htmlFor="course-toml">課程 TOML</label>
            <textarea id="course-toml" value={content} onChange={(event) => setContent(event.target.value)} spellCheck={false} />
            <div className="course-editor-actions">
              <button className="button secondary" type="button" disabled={saving || !sourceDirty} onClick={() => setContent(detail.content)}>還原</button>
              <button className="button primary" type="button" disabled={saving || !sourceDirty} onClick={() => void saveSource()}>{saving ? "驗證中…" : "儲存設定"}</button>
            </div>
          </div>}
        </> : <p className="course-editor-empty">從左側選擇課程，或建立新課程。</p>}
      </div>
    </div>
  </section>;
}
