import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { CourseDetail } from "../types";
import { CourseEditor } from "./CourseEditor";

vi.mock("../api", () => ({
  api: {
    course: vi.fn(),
    createCourse: vi.fn(),
    updateCourse: vi.fn(),
    updateCourseVocabulary: vi.fn(),
  },
}));

const detail: CourseDetail = {
  api_version: 1,
  id: "測試課",
  file: "/repo/courses/測試課.toml",
  content: '[course]\nname = "測試課"\n',
  vocabulary: {
    terms: ["UNIX"],
    glossary: [{ term: "Multics", means: "分時系統", aka: ["MUTIX"] }],
  },
};

describe("CourseEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.course).mockResolvedValue(detail);
    vi.mocked(api.updateCourse).mockResolvedValue(detail);
    vi.mocked(api.updateCourseVocabulary).mockResolvedValue(detail);
    vi.mocked(api.createCourse).mockResolvedValue(detail);
  });

  afterEach(cleanup);

  it("loads, edits, and saves course TOML", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    render(<CourseEditor
      courses={[{ id: "測試課", file: detail.file, name: "測試課" }]}
      onChanged={onChanged} onError={vi.fn()} onMessage={vi.fn()}
    />);

    await screen.findByLabelText("Whisper 常用術語（每行一個）");
    fireEvent.click(screen.getByRole("tab", { name: "原始 TOML" }));
    const editor = screen.getByLabelText("課程 TOML");
    const updated = '[course]\nname = "新名稱"\n';
    fireEvent.change(editor, { target: { value: updated } });
    fireEvent.click(screen.getByRole("button", { name: "儲存設定" }));

    await waitFor(() => expect(api.updateCourse).toHaveBeenCalledWith("測試課", updated));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("keeps a new course selected while the previous list refreshes", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    const existing = {
      ...detail,
      id: "舊課程",
      file: "/repo/courses/舊課程.toml",
      content: '[course]\nname = "舊課程"\n',
    };
    vi.mocked(api.course).mockImplementation(async (id) => (
      id === existing.id ? existing : detail
    ));
    render(<CourseEditor
      courses={[{ id: existing.id, file: existing.file, name: existing.id }]}
      onChanged={onChanged} onError={vi.fn()} onMessage={vi.fn()}
    />);

    await screen.findByLabelText("Whisper 常用術語（每行一個）");
    fireEvent.change(screen.getByLabelText("新課程名稱"), { target: { value: "測試課" } });
    fireEvent.click(screen.getByRole("button", { name: "建立課程" }));

    await waitFor(() => expect(api.createCourse).toHaveBeenCalledWith("測試課"));
    expect(onChanged).toHaveBeenCalledOnce();
    expect((await screen.findByLabelText("Whisper 常用術語（每行一個）") as HTMLTextAreaElement).value)
      .toBe("UNIX");
  });

  it("edits and saves structured terms and glossary entries", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    render(<CourseEditor
      courses={[{ id: "測試課", file: detail.file, name: "測試課" }]}
      onChanged={onChanged} onError={vi.fn()} onMessage={vi.fn()}
    />);

    const terms = await screen.findByLabelText("Whisper 常用術語（每行一個）");
    fireEvent.change(terms, { target: { value: "UNIX\nPOSIX\nUNIX" } });
    fireEvent.change(screen.getByLabelText("辨識說明"), {
      target: { value: "作業系統專案" },
    });
    fireEvent.change(screen.getByLabelText("常見錯字（每行一個）"), {
      target: { value: "MUTIX\n馬提克斯\nMUTIX" },
    });
    fireEvent.click(screen.getByRole("button", { name: "儲存術語設定" }));

    await waitFor(() => expect(api.updateCourseVocabulary).toHaveBeenCalledWith(
      "測試課",
      {
        terms: ["UNIX", "POSIX"],
        glossary: [{
          term: "Multics", means: "作業系統專案", aka: ["MUTIX", "馬提克斯"],
        }],
      },
    ));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("prevents duplicate glossary terms from being saved", async () => {
    render(<CourseEditor
      courses={[{ id: "測試課", file: detail.file, name: "測試課" }]}
      onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} onMessage={vi.fn()}
    />);

    await screen.findByLabelText("Whisper 常用術語（每行一個）");
    fireEvent.click(screen.getByRole("button", { name: "新增對照" }));
    const termInputs = screen.getAllByLabelText("正式術語");
    fireEvent.change(termInputs[1], { target: { value: "Multics" } });

    expect(screen.getByText("正式術語「Multics」重複")).toBeTruthy();
    expect(screen.getByRole("button", { name: "儲存術語設定" }).hasAttribute("disabled"))
      .toBe(true);
  });
});
