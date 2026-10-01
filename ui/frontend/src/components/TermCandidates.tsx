import { useEffect, useState } from "react";
import { api } from "../api";
import type { CourseDetail, CourseVocabulary, TermCandidatesResponse } from "../types";

type Decision = "pending" | "correct" | "misheard" | "ignore";
type Review = { decision: Decision; official: string; means: string; whisper: boolean };

interface Props {
  data: TermCandidatesResponse;
  onApplied: () => void;
  onError: (message?: string) => void;
  onMessage: (message?: string) => void;
}

function promptLength(prompt: string, terms: string[]): number {
  return prompt.trim().length + (terms.length ? `本課術語：${terms.join("、")}。`.length : 0);
}

export function TermCandidates({ data, onApplied, onError, onMessage }: Props) {
  const [review, setReview] = useState<Record<string, Review>>({});
  const [baseline, setBaseline] = useState<CourseDetail>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setReview({});
    setBaseline(undefined);
    if (!data.course_id) return;
    let active = true;
    void api.course(data.course_id).then((value) => {
      if (active) setBaseline(value);
    }).catch((reason) => {
      if (active) onError(reason instanceof Error ? reason.message : "無法讀取課程設定");
    });
    return () => { active = false; };
  }, [data.session, data.course_id, data.candidates, onError]);

  function choose(term: string, patch: Partial<Review>, explain: string) {
    setReview((current) => ({ ...current, [term]: Object.assign(
      { decision: "pending" as Decision, official: "", means: explain, whisper: false },
      current[term], patch,
    ) }));
  }

  const selected = data.candidates.filter((candidate) => review[candidate.term]?.decision !== undefined
    && review[candidate.term]?.decision !== "pending");
  const whisperTerms = [...(baseline?.vocabulary?.terms ?? [])];
  for (const candidate of selected) {
    const item = review[candidate.term];
    const term = item.decision === "misheard" ? item.official.trim() : candidate.term;
    if (item.whisper && term && !whisperTerms.includes(term)) whisperTerms.push(term);
  }
  const budget = promptLength(data.whisper_prompt_base, whisperTerms);

  async function apply() {
    if (!baseline || !selected.length || !data.course_id) return;
    onError(undefined);
    const current = await api.course(data.course_id).catch((reason) => {
      onError(reason instanceof Error ? reason.message : "無法讀取課程設定");
      return undefined;
    });
    if (!current) return;
    if (current.content !== baseline.content) {
      onError("課程設定已被其他編輯更新；請重新載入術語候選後再套用。");
      return;
    }
    const vocabulary = current.vocabulary;
    if (!vocabulary) {
      onError("課程設定格式無法編輯術語，請先修正課程檔。");
      return;
    }
    const glossary = vocabulary.glossary.map((entry) => ({ ...entry, aka: [...entry.aka] }));
    const ignored = [...(vocabulary.ignored_terms ?? [])];
    const used = new Map<string, string>();
    for (const entry of glossary) {
      for (const value of [entry.term, ...entry.aka]) used.set(value.trim().toLowerCase(), entry.term);
    }
    for (const candidate of selected) {
      const item = review[candidate.term];
      if (item.decision === "ignore") {
        for (const term of [candidate.term, ...candidate.variants.map((v) => v.term)]) {
          if (!ignored.includes(term)) ignored.push(term);
        }
        continue;
      }
      const term = item.decision === "misheard" ? item.official.trim() : candidate.term;
      const aka = [
        ...(item.decision === "misheard" ? [candidate.term] : []),
        ...candidate.variants.map((v) => v.term),
      ].filter((value) => value !== term);
      if (!term) { onError("請輸入正式術語寫法。"); return; }
      const existing = glossary.find((entry) => entry.term.toLowerCase() === term.toLowerCase());
      if ((existing && item.decision === "correct")
          || (used.has(term.toLowerCase()) && !existing)
          || aka.some((value) => used.has(value.toLowerCase()))) {
        onError(`術語或別名已存在：${term}`);
        return;
      }
      if (existing) {
        existing.aka.push(...aka);
        if (!existing.means) existing.means = item.means.trim();
      } else {
        glossary.push({ term, means: item.means.trim(), aka });
      }
      [term, ...aka].forEach((value) => used.set(value.toLowerCase(), term));
    }
    if (budget > 200 && selected.some((candidate) => review[candidate.term].whisper)) {
      onError("Whisper prompt 與術語合計超過 200 字，請取消部分 Whisper 勾選。");
      return;
    }
    const next: CourseVocabulary = { terms: whisperTerms, glossary, ignored_terms: ignored };
    setSaving(true);
    try {
      await api.updateCourseVocabulary(current.id, next);
      onMessage(`已將 ${selected.length} 組術語選擇套用至「${current.id}」`);
      onApplied();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "無法儲存術語設定");
    } finally {
      setSaving(false);
    }
  }

  return <div className="term-candidates">
    {!data.course_id || !baseline ? <p>正在讀取課程設定；請確認這堂課有對應的課程檔。</p> : null}
    {data.candidates.length === 0 ? <p>目前沒有尚未定義的術語候選。</p> : <>
      <p>逐項確認後套用到課程設定；同組近似寫法會一併加入錯字對照。聽錯時可輸入既有正式術語以新增別名。</p>
      {data.candidates.map((candidate) => {
        const item = review[candidate.term] ?? { decision: "pending", official: "", means: candidate.explain, whisper: false };
        return <article className="term-review" key={candidate.term}>
          <div><strong>{candidate.term}</strong> <small>出現 {candidate.count} 次 · {candidate.sections.length} 節</small></div>
          {candidate.variants.length > 0 && <p>近似：{candidate.variants.map((v) => v.term).join("、")}</p>}
          {candidate.flags.length > 0 && <small>{candidate.flags.join(" · ")}</small>}
          <div className="term-actions">
            <button type="button" className={item.decision === "correct" ? "active" : ""} onClick={() => choose(candidate.term, { decision: "correct" }, candidate.explain)}>✔ 正確</button>
            <button type="button" className={item.decision === "misheard" ? "active" : ""} onClick={() => choose(candidate.term, { decision: "misheard" }, candidate.explain)}>↷ 聽錯</button>
            <button type="button" className={item.decision === "ignore" ? "active" : ""} onClick={() => choose(candidate.term, { decision: "ignore" }, candidate.explain)}>✖ 永久忽略</button>
          </div>
          {item.decision === "misheard" && <label>正式寫法 <input value={item.official} onChange={(event) => choose(candidate.term, { official: event.target.value }, candidate.explain)} /></label>}
          {(item.decision === "correct" || item.decision === "misheard") && <>
            <label>術語說明 <input value={item.means} onChange={(event) => choose(candidate.term, { means: event.target.value }, candidate.explain)} /></label>
            <label><input type="checkbox" checked={item.whisper} onChange={(event) => choose(candidate.term, { whisper: event.target.checked }, candidate.explain)} /> 加入 Whisper 常用術語</label>
          </>}
        </article>;
      })}
      <div className="term-footer"><span>Whisper prompt：{budget} / 200 字</span><button className="button primary" disabled={!selected.length || !baseline || saving || (budget > 200 && selected.some((candidate) => review[candidate.term].whisper))} onClick={() => void apply()}>{saving ? "儲存中…" : "套用到課程設定"}</button></div>
    </>}
  </div>;
}
