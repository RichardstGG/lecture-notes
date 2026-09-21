import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { ModelInventory, RuntimeStatus } from "../types";
import { RunPanel } from "./RunPanel";

vi.mock("../api", () => ({
  api: {
    start: vi.fn(),
    stop: vi.fn(),
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

describe("RunPanel", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

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

    fireEvent.click(screen.getByRole("checkbox", { name: "只轉錄（不啟動總結模型）" }));
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

    const selector = screen.getByLabelText("總結模型（選填）") as HTMLSelectElement;
    expect(selector.options[0].textContent).toBe("使用課程預設（qwen3-8b）");
    expect(screen.getByRole("option", { name: "missing（未安裝）" }).hasAttribute("disabled"))
      .toBe(true);

    fireEvent.change(selector, { target: { value: "qwen3-4b" } });
    fireEvent.click(screen.getByRole("button", { name: "開始處理" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith({
      course: "測試課",
      input_file: undefined,
      model: "qwen3-4b",
      overrides: undefined,
    }));
    expect(onChanged).toHaveBeenCalledOnce();
  });
});
