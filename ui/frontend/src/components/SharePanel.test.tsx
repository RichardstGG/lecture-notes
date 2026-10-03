import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { SharePanel } from "./SharePanel";

vi.mock("../api", () => ({ api: {
  sharingNetwork: vi.fn(), sharing: vi.fn(), openSharing: vi.fn(), closeSharing: vi.fn(),
} }));
const closed = { api_version: 1, active: false, participants: [] };
const active = { api_version: 1, active: true, session_id: "first", url: "http://192.168.1.2:8766/#secret",
  participants: [{ id: "1", nickname: "小明", online: true }, { id: "2", nickname: "小華", online: false }] };

describe("SharePanel", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.sharing).mockResolvedValue(closed); vi.mocked(api.sharingNetwork).mockResolvedValue({ api_version: 1, interface: "eth0", advertise_host: "192.168.1.2" }); });
  afterEach(cleanup);
  it("opens the selected session with an explicit private address and port", async () => {
    vi.mocked(api.openSharing).mockResolvedValue(active);
    render(<SharePanel selectedId="first" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "開啟分享" }).hasAttribute("disabled")).toBe(false));
    fireEvent.change(screen.getByLabelText("連結使用的本機 IPv4"), { target: { value: "192.168.1.2" } });
    fireEvent.click(screen.getByRole("button", { name: "開啟分享" }));
    await waitFor(() => expect(api.openSharing).toHaveBeenCalledWith("first", "0.0.0.0", 8766, "192.168.1.2"));
    expect(await screen.findByDisplayValue(active.url)).toBeTruthy();
    expect(screen.getByRole("img", { name: "參與者連結 QR code" }).tagName).toBe("svg");
  });
  it("keeps the shared session and roster when the selection changes", async () => {
    vi.mocked(api.sharing).mockResolvedValue(active);
    vi.mocked(api.closeSharing).mockResolvedValue(closed);
    const { rerender } = render(<SharePanel selectedId="first" />);
    expect(await screen.findByText("小明")).toBeTruthy();
    expect(screen.getByText("在線")).toBeTruthy();
    expect(screen.getByText("離線")).toBeTruthy();
    rerender(<SharePanel selectedId="second" />);
    expect(screen.getByText(/first（不隨/)).toBeTruthy();
    expect(api.openSharing).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "關閉分享" }));
    await waitFor(() => expect(api.closeSharing).toHaveBeenCalledOnce());
    expect(await screen.findByText("將分享目前選定場次：second")).toBeTruthy();
    expect(screen.queryByRole("img", { name: "參與者連結 QR code" })).toBeNull();
  });
  it("shows bind failures without a link", async () => {
    vi.mocked(api.openSharing).mockRejectedValue(new Error("無法啟動分享"));
    render(<SharePanel selectedId="first" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "開啟分享" }).hasAttribute("disabled")).toBe(false));
    fireEvent.change(screen.getByLabelText("連結使用的本機 IPv4"), { target: { value: "192.168.1.2" } });
    fireEvent.click(screen.getByRole("button", { name: "開啟分享" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "無法啟動分享");
    expect(screen.queryByLabelText("參與者連結")).toBeNull();
  });
  it("keeps manual address entry available when route detection fails", async () => {
    vi.mocked(api.sharingNetwork).mockResolvedValue({ api_version: 1, error: "請手動輸入 IP" });
    vi.mocked(api.openSharing).mockResolvedValue(active);
    render(<SharePanel selectedId="first" />);
    expect(await screen.findByText("請手動輸入 IP")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("連結使用的本機 IPv4"), { target: { value: "192.168.1.9" } });
    fireEvent.click(screen.getByLabelText("監聽所有 IPv4 介面（0.0.0.0）"));
    fireEvent.click(screen.getByRole("button", { name: "開啟分享" }));
    await waitFor(() => expect(api.openSharing).toHaveBeenCalledWith("first", "192.168.1.9", 8766, undefined));
  });

  it("updates the QR matrix when reopening produces a new invitation", async () => {
    vi.mocked(api.sharing).mockResolvedValue(active);
    vi.mocked(api.closeSharing).mockResolvedValue(closed);
    vi.mocked(api.openSharing).mockResolvedValue({ ...active, url: active.url + "-new" });
    render(<SharePanel selectedId="first" />);
    const oldQR = (await screen.findByRole("img", { name: "參與者連結 QR code" })).innerHTML;
    fireEvent.click(screen.getByRole("button", { name: "關閉分享" }));
    fireEvent.click(await screen.findByRole("button", { name: "開啟分享" }));
    const newQR = await screen.findByRole("img", { name: "參與者連結 QR code" });
    expect(newQR.innerHTML).not.toBe(oldQR);
    expect(screen.getByDisplayValue(active.url + "-new")).toBeTruthy();
  });

});
