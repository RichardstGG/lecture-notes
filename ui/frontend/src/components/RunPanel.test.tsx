import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { Course, DeviceInventory, ModelInventory, RuntimeStatus } from "../types";
import { RunPanel } from "./RunPanel";

vi.mock("../api", () => ({
  api: {
    start: vi.fn(),
    stop: vi.fn(),
    uploadAudio: vi.fn(),
    createCourse: vi.fn(),
    courses: vi.fn(),
  },
}));

const running: RuntimeStatus = {
  schema_version: 1,
  running: true,
  course: "測試課",
};

const models: ModelInventory = {
  schema_version: 1,
  summary: {
    selected: "qwen3-8b",
    models: [
      { id: "qwen3-8b", path: "/models/8b.gguf", available: true, size_bytes: 5 * 1024 ** 3 },
      { id: "qwen3-4b", path: "/models/4b.gguf", available: true, size_bytes: 2.5 * 1024 ** 3 },
      { id: "missing", path: "/models/missing.gguf", available: false },
    ],
  },
  whisper: { selected: "large-v3-turbo", models: [] },
};

const devices: DeviceInventory = {
  api_version: 1,
  current: "default",
  default: "mic-1",
  sources: [
    { id: "mic-1", name: "Internal", description: "內建麥克風", state: "idle" },
    { id: "mic-2", name: "USB", description: "USB 麥克風", state: "running" },
  ],
};

describe("RunPanel", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it("inherits course API and disabled defaults and resets an override on course change", async () => {
    render(<RunPanel courses={[
      { id: "api-course", file: "api.toml", upstream: "lab", summary_enabled: true },
      { id: "raw-course", file: "raw.toml", model: "qwen3-8b", upstream: "local", summary_enabled: false },
    ]} models={{ ...models, summary_upstreams: { selected: "local", options: [{ id: "lab", name: "Lab", kind: "api" }] } }}
      status={{ schema_version: 1, running: false }} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} />);
    const selector = screen.getByRole("combobox", { name: "LLM總結方式" }) as HTMLSelectElement;
    expect(selector.value).toBe("api:lab");
    expect(screen.queryByText("依課程設定選取")).toBeNull();
    expect(screen.getByText("逐字稿會傳送至所選 API。")).toBeTruthy();
    expect(Array.from(selector.options).map((option) => option.value)).toEqual(["none", "local:qwen3-8b", "local:qwen3-4b", "api:lab"]);
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ course: "api-course", model: undefined, overrides: undefined })));
    expect(vi.mocked(api.start).mock.calls[0][0]).not.toHaveProperty("upstream");

    fireEvent.change(selector, { target: { value: "local:qwen3-4b" } });
    fireEvent.change(screen.getByRole("combobox", { name: "課程" }), { target: { value: "raw-course" } });
    fireEvent.click(screen.getByRole("option", { name: "raw-course" }));
    expect(selector.value).toBe("none");
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ course: "raw-course", model: undefined, overrides: undefined })));
  });

  it("creates a course, reloads the list and selects it without starting recording", async () => {
    const initial: Course[] = [{ id: "old", file: "old.toml", name: "舊課程" }];
    vi.mocked(api.courses).mockResolvedValue([...initial, { id: "new-id", file: "new-id.toml", name: "新課程" }]);
    let finish!: (value: Awaited<ReturnType<typeof api.createCourse>>) => void;
    vi.mocked(api.createCourse).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const changed = vi.fn();
    function Panel() {
      const [courses, setCourses] = useState(initial);
      return <RunPanel courses={courses} status={{ schema_version: 1, running: false }} onError={vi.fn()}
        onChanged={async () => { changed(); setCourses(await api.courses()); }} />;
    }
    render(<Panel />);
    const input = screen.getByRole("combobox", { name: "課程" }) as HTMLInputElement;
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "  新課程  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(api.createCourse).toHaveBeenCalledExactlyOnceWith("新課程");
    expect(screen.getByRole("button", { name: "開始處理" }).hasAttribute("disabled")).toBe(true);
    fireEvent.submit(input.closest("form")!);
    expect(api.start).not.toHaveBeenCalled();
    await act(async () => finish({ api_version: 1, id: "new-id", file: "new-id.toml", content: "[course]" }));
    await waitFor(() => expect(input.value).toBe("新課程"));
    expect(changed).toHaveBeenCalledOnce();
    expect(api.courses).toHaveBeenCalledOnce();
    expect(api.start).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "開始處理" }).hasAttribute("disabled")).toBe(false);
    fireEvent.focus(input);
    expect(screen.getByRole("option", { name: "新課程" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.blur(input);
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ course: "new-id" })));
  });

  it.each(["名稱不合法", "設定已存在"])("reports creation error %s and preserves the typed name", async (message) => {
    vi.mocked(api.createCourse).mockRejectedValue(new Error(message));
    function Panel() {
      const [error, setError] = useState<string>();
      return <>{error && <p role="alert">{error}</p>}<RunPanel courses={[{ id: "old", file: "old.toml", name: "舊課程" }]}
        status={{ schema_version: 1, running: false }} onChanged={vi.fn()} onError={setError} /></>;
    }
    render(<Panel />);
    const input = screen.getByRole("combobox", { name: "課程" }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: " invalid/name " } });
    fireEvent.click(screen.getByRole("option", { name: "建立「invalid/name」" }));
    expect((await screen.findByRole("alert")).textContent).toBe(message);
    expect(input.value).toBe(" invalid/name ");
    expect(api.start).not.toHaveBeenCalled();
  });

  it("shows invalid courses as disabled matches and never offers to create them", () => {
    render(<RunPanel courses={[{ id: "bad-id", file: "bad.toml", name: "壞設定", error: "invalid" }]}
      status={{ schema_version: 1, running: false }} onChanged={vi.fn()} onError={vi.fn()} />);
    const input = screen.getByRole("combobox", { name: "課程" });
    fireEvent.change(input, { target: { value: "BAD-ID" } });
    expect(screen.getByRole("option", { name: "壞設定（設定有誤）" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.queryByRole("option", { name: /建立/ })).toBeNull();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(api.createCourse).not.toHaveBeenCalled();
    expect(api.start).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "開始處理" }).hasAttribute("disabled")).toBe(true);
  });

  it("explicit local selection overrides an API or disabled course, and none drops stale choices", async () => {
    render(<RunPanel courses={[{ id: "test", file: "test.toml", upstream: "lab", summary_enabled: false }]}
      models={{ ...models, summary_upstreams: { selected: "local", options: [{ id: "lab", name: "Lab", kind: "api" }] } }}
      status={{ schema_version: 1, running: false }} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} />);
    const selector = screen.getByRole("combobox", { name: "LLM總結方式" });
    fireEvent.change(selector, { target: { value: "local:qwen3-4b" } });
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ upstream: "local", model: "qwen3-4b", overrides: { "summary.enabled": true } })));
    fireEvent.change(selector, { target: { value: "api:lab" } });
    fireEvent.change(selector, { target: { value: "none" } });
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenLastCalledWith({ course: "test", input_file: undefined, model: undefined, overrides: { "summary.enabled": false } }));
  });

  it("keeps an unavailable configured default visible without silently selecting none", () => {
    render(<RunPanel courses={[{ id: "test", file: "test.toml", model: "missing", summary_enabled: true }]}
      models={models} status={{ schema_version: 1, running: false }} onChanged={vi.fn()} onError={vi.fn()} />);
    expect((screen.getByRole("combobox", { name: "LLM總結方式" }) as HTMLSelectElement).value).toBe("local:missing");
    expect(screen.queryByText("依課程設定選取")).toBeNull();
    expect(screen.getByRole("option", { name: "本機模型 · missing（未安裝）" }).hasAttribute("disabled")).toBe(true);
  });

  it("selects a saved API upstream without needing an installed local model", async () => {
    render(<RunPanel courses={[{ id: "test", file: "test.toml" }]}
      models={{ ...models, summary: { selected: "missing", models: [] },
        summary_upstreams: { selected: "local", options: [{ id: "local", name: "本地 GPU", kind: "local" }, { id: "lab", name: "Lab GPU", kind: "api" }] } }}
      status={{ schema_version: 1, running: false }} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} />);
    fireEvent.change(screen.getByRole("combobox", { name: "LLM總結方式" }), { target: { value: "api:lab" } });
    expect(screen.queryByRole("combobox", { name: "總結模型（選填）" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ upstream: "lab", model: undefined, overrides: { "summary.enabled": true } })));
  });

  it("keeps graceful-stop feedback visible until the run actually stops", async () => {
    let resolveStop!: (value: {
      accepted: boolean; operation: string; message: string;
    }) => void;
    vi.mocked(api.stop).mockReturnValue(new Promise((resolve) => {
      resolveStop = resolve;
    }));
    const onChanged = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<RunPanel
      courses={[]} status={running} onChanged={onChanged} onError={vi.fn()}
    />);

    fireEvent.click(screen.getByRole("button", { name: "正常停止" }));
    const stopping = screen.getByRole("button", { name: "正在停止…" });
    expect(stopping.getAttribute("aria-busy")).toBe("true");
    expect(stopping.querySelector(".button-spinner")).toBeTruthy();
    expect(screen.getByRole("button", { name: "立即停止" }).hasAttribute("disabled"))
      .toBe(true);

    await act(async () => resolveStop({
      accepted: true, operation: "stop", message: "stop requested",
    }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "正在停止…" }).hasAttribute("disabled"))
      .toBe(true);
    expect(screen.getByRole("button", { name: "立即停止" }).hasAttribute("disabled"))
      .toBe(false);

    rerender(<RunPanel
      courses={[]} status={{ schema_version: 1, running: false }}
      onChanged={onChanged} onError={vi.fn()}
    />);
    expect(screen.getByRole("button", { name: "開始處理" })).toBeTruthy();
  });

  it("starts a transcribe-only run through the existing override contract", async () => {
    vi.mocked(api.start).mockResolvedValue({
      accepted: true, operation: "run", message: "started",
    });
    const onChanged = vi.fn().mockResolvedValue(undefined);
    render(<RunPanel
      courses={[{ id: "測試課", file: "/courses/測試課.toml", name: "測試課" }]}
      status={{ schema_version: 1, running: false }}
      onChanged={onChanged}
      onError={vi.fn()}
    />);

    fireEvent.change(screen.getByRole("combobox", { name: "LLM總結方式" }), { target: { value: "none" } });
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith({
      course: "測試課",
      input_file: undefined,
      model: undefined,
      overrides: { "summary.enabled": false },
    }));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("selects an available discovered summary model", async () => {
    vi.mocked(api.start).mockResolvedValue({
      accepted: true, operation: "run", message: "started",
    });
    const onChanged = vi.fn().mockResolvedValue(undefined);
    render(<RunPanel
      courses={[{
        id: "測試課", file: "/courses/測試課.toml", name: "測試課", model: "qwen3-8b",
      }]}
      models={models}
      status={{ schema_version: 1, running: false }}
      onChanged={onChanged}
      onError={vi.fn()}
    />);

    const selector = screen.getByLabelText("LLM總結方式") as HTMLSelectElement;
    expect(selector.options[0].textContent).toBe("不總結");
    expect(selector.value).toBe("local:qwen3-8b");
    expect(screen.queryByRole("option", { name: /missing/ })).toBeNull();

    fireEvent.change(selector, { target: { value: "local:qwen3-4b" } });
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith({
      course: "測試課",
      input_file: undefined,
      model: "qwen3-4b",
      upstream: "local",
      overrides: { "summary.enabled": true },
    }));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("starts live recording with an explicitly selected microphone", async () => {
    vi.mocked(api.start).mockResolvedValue({
      accepted: true, operation: "run", message: "started",
    });
    render(<RunPanel
      courses={[{ id: "測試課", file: "/courses/測試課.toml", name: "測試課" }]}
      devices={devices}
      status={{ schema_version: 1, running: false }}
      onChanged={vi.fn().mockResolvedValue(undefined)}
      onError={vi.fn()}
    />);

    fireEvent.change(screen.getByLabelText("麥克風（選填）"), {
      target: { value: "mic-2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith({
      course: "測試課",
      input_file: undefined,
      model: undefined,
      source: "mic-2",
      overrides: undefined,
    }));
  });

  it("prepares a browser-selected audio file before starting file mode", async () => {
    const file = new File(["audio-data"], "課堂錄音.ogg", { type: "audio/ogg" });
    vi.mocked(api.uploadAudio).mockResolvedValue({
      api_version: 1,
      name: file.name,
      path: "/tmp/lecture-notes-ui/uploads/selected.ogg",
      size_bytes: file.size,
    });
    vi.mocked(api.start).mockResolvedValue({
      accepted: true, operation: "run", message: "started",
    });
    render(<RunPanel
      courses={[{ id: "測試課", file: "/courses/測試課.toml", name: "測試課" }]}
      status={{ schema_version: 1, running: false }}
      onChanged={vi.fn().mockResolvedValue(undefined)}
      onError={vi.fn()}
    />);

    fireEvent.click(screen.getByRole("button", { name: "處理音檔" }));
    fireEvent.change(screen.getByLabelText("選擇音檔"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(screen.getByText("課堂錄音.ogg")).toBeTruthy());
    expect(api.uploadAudio).toHaveBeenCalledWith(file);
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith({
      course: "測試課",
      input_file: "/tmp/lecture-notes-ui/uploads/selected.ogg",
      model: undefined,
      overrides: undefined,
    }));
  });

  it("keeps manual local paths as a file-mode fallback", async () => {
    vi.mocked(api.start).mockResolvedValue({
      accepted: true, operation: "run", message: "started",
    });
    render(<RunPanel
      courses={[{ id: "測試課", file: "/courses/測試課.toml", name: "測試課" }]}
      status={{ schema_version: 1, running: false }}
      onChanged={vi.fn().mockResolvedValue(undefined)}
      onError={vi.fn()}
    />);

    fireEvent.click(screen.getByRole("button", { name: "處理音檔" }));
    fireEvent.click(screen.getByRole("button", { name: "手動輸入路徑" }));
    fireEvent.change(screen.getByLabelText("本機音檔路徑"), {
      target: { value: "/recordings/existing.flac" },
    });
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith({
      course: "測試課",
      input_file: "/recordings/existing.flac",
      model: undefined,
      overrides: undefined,
    }));
  });
});
