import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { DeviceInventory, DoctorResult } from "../types";
import { LocalSettings } from "./LocalSettings";

vi.mock("../api", () => ({
  api: {
    selectDevice: vi.fn(),
    testDevice: vi.fn(),
    doctor: vi.fn(),
  },
}));

const devices: DeviceInventory = {
  api_version: 1,
  current: "default",
  default: "mic-1",
  sources: [
    { id: "mic-1", name: "Internal", description: "內建麥克風", state: "idle" },
    { id: "mic-2", name: "USB", description: "USB 麥克風", state: "running" },
  ],
};

const diagnostics: DoctorResult = {
  api_version: 1,
  course: "測試課",
  microphone_test: true,
  summary: { ok: 1, warnings: 1, failures: 1 },
  items: [
    { status: "✔", name: "Python", detail: "3.13" },
    { status: "⚠", name: "GPU", detail: "CPU fallback" },
    { status: "✖", name: "麥克風", detail: "missing" },
  ],
};

function renderSettings(overrides = {}) {
  return render(<LocalSettings
    courses={[{ id: "測試課", name: "測試課", file: "/courses/測試課.toml" }]}
    devices={devices}
    devicesLoading={false}
    onDevicesChanged={vi.fn()}
    onRefreshDevices={vi.fn().mockResolvedValue(undefined)}
    onError={vi.fn()}
    onMessage={vi.fn()}
    {...overrides}
  />);
}

describe("LocalSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.selectDevice).mockResolvedValue({ ...devices, current: "mic-2" });
    vi.mocked(api.testDevice).mockResolvedValue({
      api_version: 1, source: "mic-2", message: "音量正常",
    });
    vi.mocked(api.doctor).mockResolvedValue(diagnostics);
  });
  afterEach(cleanup);

  it("tests and saves a microphone through the CLI-backed API", async () => {
    const onDevicesChanged = vi.fn();
    const onMessage = vi.fn();
    renderSettings({ onDevicesChanged, onMessage });

    fireEvent.change(screen.getByLabelText("錄音來源"), { target: { value: "mic-2" } });
    fireEvent.click(screen.getByRole("button", { name: "測試 3 秒" }));
    await waitFor(() => expect(api.testDevice).toHaveBeenCalledWith("mic-2"));
    expect(onMessage).toHaveBeenCalledWith("音量正常");

    fireEvent.click(screen.getByRole("button", { name: "儲存為本機預設" }));
    await waitFor(() => expect(api.selectDevice).toHaveBeenCalledWith("mic-2"));
    expect(onDevicesChanged).toHaveBeenCalledWith({ ...devices, current: "mic-2" });
  });

  it("runs course-aware doctor with the optional microphone test", async () => {
    renderSettings();
    fireEvent.change(screen.getByLabelText("課程設定（選填）"), {
      target: { value: "測試課" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "同時錄音 3 秒" }));
    fireEvent.click(screen.getByRole("button", { name: "執行環境檢查" }));

    await waitFor(() => expect(api.doctor).toHaveBeenCalledWith("測試課", true));
    expect(screen.getByText("1 錯誤 · 1 警告")).toBeTruthy();
    expect(screen.getByLabelText("環境檢查結果").textContent).toContain("麥克風");
  });
});
