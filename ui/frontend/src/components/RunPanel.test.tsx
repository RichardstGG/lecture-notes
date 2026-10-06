import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { DeviceInventory, ModelInventory, RuntimeStatus } from "../types";
import { RunPanel } from "./RunPanel";

vi.mock("../api", () => ({
  api: {
    start: vi.fn(),
    stop: vi.fn(),
    uploadAudio: vi.fn(),
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
    const selector = screen.getByRole("combobox", { name: "總結方式" }) as HTMLSelectElement;
    expect(selector.value).toBe("api:lab");
    expect(Array.from(selector.options).map((option) => option.value)).toEqual(["none", "local:qwen3-8b", "local:qwen3-4b", "api:lab"]);
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ course: "api-course", model: undefined, overrides: undefined })));
    expect(vi.mocked(api.start).mock.calls[0][0]).not.toHaveProperty("upstream");

    fireEvent.change(selector, { target: { value: "local:qwen3-4b" } });
    fireEvent.change(screen.getByRole("combobox", { name: "課程" }), { target: { value: "raw-course" } });
    expect(selector.value).toBe("none");
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ course: "raw-course", model: undefined, overrides: undefined })));
  });

  it("explicit local selection overrides an API or disabled course, and none drops stale choices", async () => {
    render(<RunPanel courses={[{ id: "test", file: "test.toml", upstream: "lab", summary_enabled: false }]}
      models={{ ...models, summary_upstreams: { selected: "local", options: [{ id: "lab", name: "Lab", kind: "api" }] } }}
      status={{ schema_version: 1, running: false }} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} />);
    const selector = screen.getByRole("combobox", { name: "總結方式" });
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
    expect((screen.getByRole("combobox", { name: "總結方式" }) as HTMLSelectElement).value).toBe("local:missing");
    expect(screen.getByRole("option", { name: "本機模型 · missing（未安裝）" }).hasAttribute("disabled")).toBe(true);
  });

  it("selects a saved API upstream without needing an installed local model", async () => {
    render(<RunPanel courses={[{ id: "test", file: "test.toml" }]}
      models={{ ...models, summary: { selected: "missing", models: [] },
        summary_upstreams: { selected: "local", options: [{ id: "local", name: "本地 GPU", kind: "local" }, { id: "lab", name: "Lab GPU", kind: "api" }] } }}
      status={{ schema_version: 1, running: false }} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} />);
    fireEvent.change(screen.getByRole("combobox", { name: "總結方式" }), { target: { value: "api:lab" } });
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

    fireEvent.change(screen.getByRole("combobox", { name: "總結方式" }), { target: { value: "none" } });
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

    const selector = screen.getByLabelText("總結方式") as HTMLSelectElement;
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
