// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { MeetingWorkbench } from "./MeetingWorkbench";

vi.mock("../api", () => ({ api: {
  captureCapabilities: vi.fn().mockResolvedValue({
    schema_version: 1,
    single: { available: true, reason_code: null, message: "單來源" },
    dual: { available: false, reason_code: "engine_not_integrated", message: "雙音源引擎與會議流程尚未整合" },
  }), session: vi.fn() } }));
vi.mock("./SharePanel", () => ({ SharePanel: ({ selectedId }: { selectedId?: string }) => <div>分享目標：{selectedId || "無"}</div> }));

describe("meeting workbench preview", () => {
  beforeEach(() => {
    vi.stubGlobal("EventSource", class { addEventListener() {} close() {} });
    vi.mocked(api.session).mockResolvedValue({
      api_version: 1,
      session: { id: "meetings/one", work_type: "meeting", course: "設計會議", started_at: "", updated_at: "", has_transcript: true, has_notes: false, has_recording: true },
      transcript: { content: "真實原稿", size_bytes: 12 },
      notes: { content: "", size_bytes: 0 },
      speaker_transcript: { content: "S01: 真實發言", size_bytes: 16 },
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("keeps dual recording disabled and never invents meters or devices", async () => {
    render(<MeetingWorkbench runtime={{ schema_version: 1, running: false }} />);
    fireEvent.change(screen.getByLabelText("錄音來源模式（預覽）"), { target: { value: "dual" } });
    await screen.findByText("雙音源引擎與會議流程尚未整合");
    expect(screen.getByLabelText("系統輸出裝置").hasAttribute("disabled")).toBe(true);
    expect(screen.getByLabelText("麥克風裝置").hasAttribute("disabled")).toBe(true);
    expect(screen.getByLabelText("各路音訊狀態").textContent).toContain("麥克風：未開始 · 音量未知");
    expect(screen.getByRole("button", { name: "開始錄音（待串接）" }).hasAttribute("disabled")).toBe(true);
  });

  it("fails closed if capability lookup fails", async () => {
    vi.mocked(api.captureCapabilities).mockRejectedValueOnce(new Error("offline"));
    render(<MeetingWorkbench runtime={{ schema_version: 1, running: false }} />);
    fireEvent.change(screen.getByLabelText("錄音來源模式（預覽）"), { target: { value: "dual" } });
    await screen.findByText("無法查詢雙音源能力；錄音保持停用");
    expect(screen.getByRole("button", { name: "開始錄音（待串接）" }).hasAttribute("disabled")).toBe(true);
  });

  it("labels mock controls and collapses six short speakers in the 10/4 example", () => {
    render(<MeetingWorkbench runtime={{ schema_version: 1, running: false }} />);
    expect(screen.getByText("會議工作台預覽")).toBeTruthy();
    expect(screen.getByRole("button", { name: "開始錄音（待串接）" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByLabelText("預計參與人數（必填）")).toBeTruthy();
    expect(screen.getByText("其他短發言 · 6 位 展開")).toBeTruthy();
    expect(screen.queryByText("S05")).toBeNull();
    fireEvent.click(screen.getByText("其他短發言 · 6 位 展開"));
    expect(screen.getByText("S05")).toBeTruthy();
  });

  it("shows a numeric progress preview without calling the real process", () => {
    render(<MeetingWorkbench runtime={{ schema_version: 1, running: true, work_type: "lecture" }} />);
    fireEvent.click(screen.getByRole("button", { name: "辨識中" }));
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("47");
    expect(screen.getByText(/要求 10 位 · 已找到 4 位主要發言者/)).toBeTruthy();
    expect(screen.getByText(/兩個工作台共用單一工作限制/)).toBeTruthy();
  });

  it("selects only real meeting sessions for sharing", () => {
    render(<MeetingWorkbench runtime={{ schema_version: 1, running: false }} sessions={[
      { id: "lecture/one", work_type: "lecture", started_at: "", updated_at: "", has_transcript: true, has_notes: true, has_recording: true },
      { id: "meetings/one", work_type: "meeting", course: "設計會議", started_at: "", updated_at: "", has_transcript: true, has_notes: false, has_recording: true },
    ]} />);
    expect(screen.getByText("分享目標：meetings/one")).toBeTruthy();
    expect(screen.queryByRole("option", { name: "lecture/one" })).toBeNull();
  });

  it("renders real meeting transcripts separately from the 10/4 preview", async () => {
    render(<MeetingWorkbench runtime={{ schema_version: 1, running: false }} sessions={[
      { id: "meetings/one", work_type: "meeting", course: "設計會議", started_at: "", updated_at: "", has_transcript: true, has_notes: false, has_recording: true, has_speaker_transcript: true },
    ]} />);
    const history = document.getElementById("meeting-history")!;
    await within(history).findByText("真實原稿");
    fireEvent.click(within(history).getByRole("tab", { name: "帶發言者逐字稿" }));
    expect(within(history).getByText("S01: 真實發言")).toBeTruthy();
    expect(within(history).queryByText("S05")).toBeNull();
    expect(api.session).toHaveBeenCalledWith("meetings/one");
  });

  it("replaces the visible speaker transcript when the session stream switches generation", async () => {
    class Stream {
      static current: Stream;
      listeners = new Map<string, (event: MessageEvent<string>) => void>();
      constructor() { Stream.current = this; }
      addEventListener(type: string, listener: (event: MessageEvent<string>) => void) { this.listeners.set(type, listener); }
      close() {}
      emit(type: string, value: unknown) { this.listeners.get(type)?.({ data: JSON.stringify(value) } as MessageEvent<string>); }
    }
    vi.stubGlobal("EventSource", Stream);
    render(<MeetingWorkbench runtime={{ schema_version: 1, running: false }} sessions={[
      { id: "meetings/one", work_type: "meeting", started_at: "", updated_at: "", has_transcript: true, has_notes: false, has_recording: true },
    ]} />);
    const history = document.getElementById("meeting-history")!;
    await within(history).findByText("真實原稿");
    fireEvent.click(within(history).getByRole("tab", { name: "帶發言者逐字稿" }));
    Stream.current.emit("content", { target: "speaker_transcript", operation: "replace", content: "S02: 新版本", size_bytes: 15 });
    await waitFor(() => expect(within(history).getByText("S02: 新版本")).toBeTruthy());
    expect(within(history).queryByText("S01: 真實發言")).toBeNull();
  });

  it("shows real stage progress and omits a percentage when duration is unknown", async () => {
    render(<MeetingWorkbench runtime={{ schema_version: 2, running: true, work_type: "meeting", mode: "diarize",
      status: { phase: "diarizing", diarization: { stage: "segmentation", processed_seconds: 120, total_seconds: null, requested_speakers: 10, speakers_found: 4 } },
    }} />);
    const live = screen.getByRole("region", { name: "目前會議工作" });
    expect(within(live).getByText(/發言者辨識 · 已處理 2:00/)).toBeTruthy();
    expect(within(live).getByText("要求 10 位 · 已找到 4 個代號")).toBeTruthy();
    expect(within(live).queryByRole("progressbar")).toBeNull();
    await waitFor(() => expect(api.session).not.toHaveBeenCalled());
  });
});
