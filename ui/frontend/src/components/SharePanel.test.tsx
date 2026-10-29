import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { SharePanel } from "./SharePanel";
const { readFileSync } = await vi.importActual<{
  readFileSync: (path: string, encoding: string) => string;
}>("node:fs");
const { cwd } = await vi.importActual<{ cwd: () => string }>("node:process");
const readerHtml = readFileSync(`${cwd()}/../backend/share_static/index.html`, "utf8");
const readerScript = readFileSync(`${cwd()}/../backend/share_static/reader.js`, "utf8");

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
    await waitFor(() => expect(api.openSharing).toHaveBeenCalledWith("first", "0.0.0.0", 8766, "192.168.1.2", false));
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
    await waitFor(() => expect(api.openSharing).toHaveBeenCalledWith("first", "192.168.1.9", 8766, undefined, false));
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

  it("labels meeting sharing as two transcripts and keeps the existing open flow", async () => {
    vi.mocked(api.openSharing).mockResolvedValue({ ...active, work_type: "meeting", session_id: "meetings/design" });
    render(<SharePanel selectedId="meetings/design" workType="meeting" />);
    expect(screen.getByText(/原逐字稿與帶發言者逐字稿/)).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("button", { name: "開啟分享" }).hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "開啟分享" }));
    await waitFor(() => expect(api.openSharing).toHaveBeenCalledWith("meetings/design", "0.0.0.0", 8766, "192.168.1.2", false));
  });

  it("shares audio only after opting in and locks the option while active", async () => {
    vi.mocked(api.openSharing).mockResolvedValue({ ...active, share_audio: true });
    render(<SharePanel selectedId="first" />);
    const option = screen.getByRole("checkbox", { name: "同時分享錄音（訪客可播放原始音訊）" }) as HTMLInputElement;
    expect(option.checked).toBe(false);
    await waitFor(() => expect(screen.getByRole("button", { name: "開啟分享" }).hasAttribute("disabled")).toBe(false));
    fireEvent.click(option);
    fireEvent.click(screen.getByRole("button", { name: "開啟分享" }));
    await waitFor(() => expect(api.openSharing).toHaveBeenCalledWith("first", "0.0.0.0", 8766, "192.168.1.2", true));
    expect(await screen.findByText("錄音分享：已開啟（有可用錄音時訪客可播放）")).toBeTruthy();
    expect(option.checked).toBe(true);
    expect(option.disabled).toBe(true);
  });

  it("displays the server audio setting when resuming an active share", async () => {
    vi.mocked(api.sharing).mockResolvedValue({ ...active, share_audio: false });
    render(<SharePanel selectedId="first" />);
    expect(await screen.findByText("錄音分享：未開啟")).toBeTruthy();
    const option = screen.getByRole("checkbox", { name: "同時分享錄音（訪客可播放原始音訊）" }) as HTMLInputElement;
    expect(option.checked).toBe(false);
    expect(option.disabled).toBe(true);
  });

});

// Keep reader audio coverage with the issue's scoped sharing UI tests.
describe("shared reader sentence playback", () => {
  const snapshot = {
    work_type: "lecture", audio: true, version: "v1", captured_at: "2026-10-29T00:00:00Z",
    transcript: { content: '## 00:00:00\n`00:00:01` <img src="evil">\n[GAP]\n`00:01:10` 中間\n`00:02:00` 結尾' },
    notes: { content: "`00:00:01` 筆記維持文字" },
  };
  const response = (value: unknown) => new Response(JSON.stringify(value));
  const flush = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); };
  const player = () => document.getElementById("audio") as HTMLAudioElement;
  const buttons = () => document.querySelectorAll<HTMLButtonElement>("#transcript .sentence-play");

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = readerHtml;
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    Object.defineProperty(player(), "readyState", { configurable: true, value: 1 });
    Object.defineProperty(player(), "duration", { configurable: true, value: 180 });
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(response(snapshot))));
  });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  it("renders safe sentence buttons, seeks, stops at the next sentence and plays the last to the end", async () => {
    new Function(readerScript)();
    await flush();
    expect(buttons()).toHaveLength(3);
    expect(document.querySelector("#transcript img")).toBeNull();
    expect(document.getElementById("transcript")!.textContent).toContain("## 00:00:00");
    expect(document.getElementById("transcript")!.textContent).toContain("[GAP]");
    expect(document.querySelectorAll("#secondary button")).toHaveLength(0);
    buttons()[0].click();
    expect(player().currentTime).toBe(1);
    expect(player().play).toHaveBeenCalledOnce();
    vi.mocked(player().pause).mockClear();
    player().currentTime = 69;
    player().dispatchEvent(new Event("timeupdate"));
    expect(player().pause).not.toHaveBeenCalled();
    player().currentTime = 70;
    player().dispatchEvent(new Event("timeupdate"));
    expect(player().pause).toHaveBeenCalledOnce();
    buttons()[2].click();
    vi.mocked(player().pause).mockClear();
    player().currentTime = 179;
    player().dispatchEvent(new Event("timeupdate"));
    expect(player().pause).not.toHaveBeenCalled();
  });

  it("preserves unchanged DOM and playing media during transcript updates", async () => {
    new Function(readerScript)();
    await flush();
    const firstButton = buttons()[0];
    firstButton.click();
    const media = player();
    const src = media.src;
    vi.mocked(media.pause).mockClear();
    vi.mocked(media.load).mockClear();
    await vi.advanceTimersByTimeAsync(2000);
    expect(buttons()[0]).toBe(firstButton);
    vi.mocked(fetch).mockImplementation(() => Promise.resolve(response({
      ...snapshot, transcript: { content: snapshot.transcript.content + "\n新增內容" },
    })));
    await vi.advanceTimersByTimeAsync(2000);
    expect(buttons()[0]).not.toBe(firstButton);
    expect(player()).toBe(media);
    expect(media.src).toBe(src);
    expect(media.currentTime).toBe(1);
    expect(media.pause).not.toHaveBeenCalled();
    expect(media.load).not.toHaveBeenCalled();
  });

  it("reloads a growing recording with a fresh version query before seeking", async () => {
    Object.defineProperty(player(), "duration", { configurable: true, value: 60 });
    new Function(readerScript)();
    await flush();
    buttons()[1].click();
    const initialSrc = player().src;
    expect(initialSrc).toContain("/share/v1/audio?v=v1-");
    expect(player().load).toHaveBeenCalledOnce();
    expect(player().play).not.toHaveBeenCalled();
    player().dispatchEvent(new Event("loadedmetadata"));
    expect(document.getElementById("audio-status")!.textContent).toContain("尚未包含");
    buttons()[1].click();
    expect(player().src).not.toBe(initialSrc);
    Object.defineProperty(player(), "duration", { configurable: true, value: 180 });
    player().dispatchEvent(new Event("loadedmetadata"));
    expect(player().currentTime).toBe(70);
    expect(player().play).toHaveBeenCalledOnce();
    player().currentTime = 120;
    vi.mocked(player().pause).mockClear();
    player().dispatchEvent(new Event("timeupdate"));
    expect(player().pause).toHaveBeenCalledOnce();
  });

  it("hides media without audio, supports timestamped speaker text and reports unsupported media", async () => {
    vi.mocked(fetch).mockImplementation(() => Promise.resolve(response({ ...snapshot, audio: false })));
    new Function(readerScript)();
    await flush();
    expect(document.getElementById("audio-player")!.hidden).toBe(true);
    expect(buttons()).toHaveLength(0);
    vi.mocked(fetch).mockImplementation(() => Promise.resolve(response({
      ...snapshot, work_type: "meeting", speaker_transcript: { content: "`00:00:05` S01: 大家好\n[00:00:10.000] S02: 純文字" },
    })));
    await vi.advanceTimersByTimeAsync(2000);
    expect(document.getElementById("audio-player")!.hidden).toBe(false);
    expect(document.querySelectorAll("#secondary button")).toHaveLength(1);
    player().dispatchEvent(new Event("error"));
    expect(document.getElementById("audio-status")!.textContent).toContain("瀏覽器可能不支援");
    vi.mocked(fetch).mockImplementation(() => Promise.resolve(response({ ...snapshot, audio: false })));
    await vi.advanceTimersByTimeAsync(2000);
    expect(player().pause).toHaveBeenCalled();
    expect(player().hasAttribute("src")).toBe(false);
    expect(buttons()).toHaveLength(0);
  });
});
