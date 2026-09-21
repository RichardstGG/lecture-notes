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
  },
}));

const detail: CourseDetail = {
  api_version: 1,
  id: "測試課",
  file: "/repo/courses/測試課.toml",
  content: '[course]\nname = "測試課"\n',
};

describe("CourseEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.course).mockResolvedValue(detail);
    vi.mocked(api.updateCourse).mockResolvedValue(detail);
    vi.mocked(api.createCourse).mockResolvedValue(detail);
  });

  afterEach(cleanup);

  it("loads, edits, and saves course TOML", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    render(<CourseEditor
      courses={[{ id: "測試課", file: detail.file, name: "測試課" }]}
      onChanged={onChanged} onError={vi.fn()} onMessage={vi.fn()}
    />);

    const editor = await screen.findByLabelText("課程 TOML");
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

    await waitFor(() => expect(
      (screen.getByLabelText("課程 TOML") as HTMLTextAreaElement).value,
    ).toBe(existing.content));
    fireEvent.change(screen.getByLabelText("新課程名稱"), { target: { value: "測試課" } });
    fireEvent.click(screen.getByRole("button", { name: "建立課程" }));

    await waitFor(() => expect(api.createCourse).toHaveBeenCalledWith("測試課"));
    expect(onChanged).toHaveBeenCalledOnce();
    expect((await screen.findByLabelText("課程 TOML") as HTMLTextAreaElement).value)
      .toBe(detail.content);
  });
});
