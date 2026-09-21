import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { RuntimeStatus } from "../types";
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
});
