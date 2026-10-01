import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { CourseDetail, TermCandidatesResponse } from "../types";
import { TermCandidates } from "./TermCandidates";

vi.mock("../api", () => ({ api: { course: vi.fn(), updateCourseVocabulary: vi.fn() } }));

const detail: CourseDetail = {
  api_version: 1, id: "測試課", file: "/courses/測試課.toml",
  content: '[course]\nname = "測試課"\n',
  vocabulary: { terms: ["UNIX"], ignored_terms: ["小考"],
    glossary: [{ term: "POSIX", means: "標準", aka: ["波西克斯"] }] },
};
const data: TermCandidatesResponse = {
  schema_version: 1, session: "/outputs/測試課", course: "測試課", course_id: "測試課",
  course_file: detail.file, whisper_prompt_base: "課堂內容。", defined: { terms: 1, glossary: 1 },
  candidates: [{ term: "大眾電容", count: 1, sections: ["00:05:00"],
    explain: "可能指元件", asr_original: "", verified: true, flags: ["hedged"],
    variants: [{ term: "大眾電容器", count: 1, sections: ["00:10:00"], similarity: 0.8 }] }],
};

describe("TermCandidates", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.course).mockResolvedValue(detail);
    vi.mocked(api.updateCourseVocabulary).mockResolvedValue(detail);
  });
  afterEach(cleanup);

  it("merges a corrected candidate with current course vocabulary", async () => {
    const onApplied = vi.fn();
    render(<TermCandidates data={data} onApplied={onApplied} onError={vi.fn()} onMessage={vi.fn()} />);
    await screen.findByText("大眾電容");
    await waitFor(() => expect(api.course).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "↷ 聽錯" }));
    fireEvent.change(screen.getByLabelText("正式寫法"), { target: { value: "電容" } });
    fireEvent.click(screen.getByRole("button", { name: "套用到課程設定" }));
    await waitFor(() => expect(api.updateCourseVocabulary).toHaveBeenCalledWith("測試課", {
      terms: ["UNIX"], ignored_terms: ["小考"], glossary: [
        { term: "POSIX", means: "標準", aka: ["波西克斯"] },
        { term: "電容", means: "可能指元件", aka: ["大眾電容", "大眾電容器"] },
      ],
    }));
    expect(onApplied).toHaveBeenCalledOnce();
  });

  it("adds a misheard form to an existing official term", async () => {
    render(<TermCandidates data={data} onApplied={vi.fn()} onError={vi.fn()} onMessage={vi.fn()} />);
    await waitFor(() => expect(api.course).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "↷ 聽錯" }));
    fireEvent.change(screen.getByLabelText("正式寫法"), { target: { value: "POSIX" } });
    fireEvent.click(screen.getByRole("button", { name: "套用到課程設定" }));
    await waitFor(() => expect(api.updateCourseVocabulary).toHaveBeenCalledWith("測試課", {
      terms: ["UNIX"], ignored_terms: ["小考"],
      glossary: [{ term: "POSIX", means: "標準", aka: ["波西克斯", "大眾電容", "大眾電容器"] }],
    }));
  });

  it("stops when the course was edited in another view", async () => {
    vi.mocked(api.course).mockResolvedValueOnce(detail).mockResolvedValueOnce({
      ...detail, content: detail.content + "# changed\n",
    });
    const onError = vi.fn();
    render(<TermCandidates data={data} onApplied={vi.fn()} onError={onError} onMessage={vi.fn()} />);
    await waitFor(() => expect(api.course).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "✔ 正確" }));
    fireEvent.click(screen.getByRole("button", { name: "套用到課程設定" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining("重新載入")));
    expect(api.updateCourseVocabulary).not.toHaveBeenCalled();
  });

  it("blocks duplicate terms and a Whisper prompt over 200 characters", async () => {
    const duplicate = { ...data, candidates: [{ ...data.candidates[0], term: "POSIX" }] };
    const onError = vi.fn();
    const { rerender } = render(<TermCandidates data={duplicate} onApplied={vi.fn()} onError={onError} onMessage={vi.fn()} />);
    await waitFor(() => expect(api.course).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "✔ 正確" }));
    fireEvent.click(screen.getByRole("button", { name: "套用到課程設定" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining("已存在")));
    expect(api.updateCourseVocabulary).not.toHaveBeenCalled();

    rerender(<TermCandidates data={{ ...data, whisper_prompt_base: "字".repeat(195) }}
      onApplied={vi.fn()} onError={onError} onMessage={vi.fn()} />);
    await waitFor(() => expect(api.course).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole("button", { name: "✔ 正確" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "加入 Whisper 常用術語" }));
    expect(screen.getByRole("button", { name: "套用到課程設定" }).hasAttribute("disabled")).toBe(true);
  });
});
