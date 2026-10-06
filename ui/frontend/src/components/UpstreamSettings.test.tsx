import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { UpstreamSettings } from "./UpstreamSettings";

vi.mock("../api", () => ({ api: {
  summaryUpstreams: vi.fn(), createSummaryUpstream: vi.fn(),
  updateSummaryUpstream: vi.fn(), deleteSummaryUpstream: vi.fn(),
} }));

const saved = { id: "lab", name: "內網 GPU", kind: "api" as const, auth_mode: "api_key" as const };

function renderSettings(overrides = {}) {
  return render(<UpstreamSettings
    onChanged={vi.fn().mockResolvedValue(undefined)}
    onError={vi.fn()} onMessage={vi.fn()} {...overrides}
  />);
}

describe("UpstreamSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.summaryUpstreams).mockResolvedValue({ api_version: 1, upstreams: [saved] });
    vi.mocked(api.createSummaryUpstream).mockResolvedValue(saved);
    vi.mocked(api.updateSummaryUpstream).mockResolvedValue(saved);
    vi.mocked(api.deleteSummaryUpstream).mockResolvedValue({ api_version: 1, deleted: "lab" });
  });
  afterEach(cleanup);

  it("creates an API-key upstream and clears the write-only secret", async () => {
    renderSettings();
    await screen.findByText("內網 GPU");
    fireEvent.change(screen.getByLabelText("上游 ID"), { target: { value: "backup" } });
    fireEvent.change(screen.getByLabelText("上游顯示名稱"), { target: { value: "備用 GPU" } });
    fireEvent.change(screen.getByLabelText("上游 Base URL"), { target: { value: "https://gpu.example.invalid/v1" } });
    fireEvent.change(screen.getByLabelText("上游模型 ID"), { target: { value: "model-30b" } });
    fireEvent.change(screen.getByLabelText("上游認證方式"), { target: { value: "api_key" } });
    fireEvent.change(screen.getByLabelText("上游 API key"), { target: { value: "secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "新增上游" }));
    await waitFor(() => expect(api.createSummaryUpstream).toHaveBeenCalledWith({
      id: "backup", name: "備用 GPU", base_url: "https://gpu.example.invalid/v1",
      model: "model-30b", auth_mode: "api_key", api_key: "secret-key",
      api_key_env: undefined,
    }));
    await waitFor(() => expect((screen.getByLabelText("上游 ID") as HTMLInputElement).value).toBe(""));
    expect(screen.queryByLabelText("上游 API key")).toBeNull();
  });

  it("requires all connection fields again when replacing an existing upstream", async () => {
    const onMessage = vi.fn();
    renderSettings({ onMessage });
    await screen.findByText("內網 GPU");
    fireEvent.click(screen.getByRole("button", { name: "覆寫" }));
    expect((screen.getByLabelText("上游 ID") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("上游 Base URL") as HTMLInputElement).value).toBe("");
    expect(onMessage).toHaveBeenCalledWith(expect.stringContaining("不會從後端讀回"));
  });

  it("deletes only after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderSettings();
    await screen.findByText("內網 GPU");
    fireEvent.click(screen.getByRole("button", { name: "刪除" }));
    await waitFor(() => expect(api.deleteSummaryUpstream).toHaveBeenCalledWith("lab"));
  });
});
