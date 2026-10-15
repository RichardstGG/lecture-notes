// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { RuntimeStatus, SessionDetail } from "../types";
import { MeetingWorkbench } from "./MeetingWorkbench";

vi.mock("../api", () => ({ api: {
  meetings: vi.fn(), meeting: vi.fn(), createMeeting: vi.fn(), updateMeeting: vi.fn(),
  startMeeting: vi.fn(), diarize: vi.fn(), stop: vi.fn(), uploadAudio: vi.fn(), session: vi.fn(),
} }));
vi.mock("./SharePanel", () => ({ SharePanel: ({ selectedId }: { selectedId?: string }) => <div>分享目標：{selectedId || "無"}</div> }));
const idle: RuntimeStatus = { schema_version: 2, running: false };
const summary = { id: "meetings/one", work_type: "meeting", course: "設計會議", started_at: "", updated_at: "", has_transcript: true, has_notes: false, has_recording: true, phase: "done" };
const detail: SessionDetail = {
  api_version: 1, session: summary, transcript: { content: "真實原稿", size_bytes: 12 }, notes: { content: "", size_bytes: 0 },
  speaker_transcript: { content: "", size_bytes: 0 },
  source: { kind: "import", original_name: "meeting.ogg", duration_seconds: 120, requested_speakers: 3 },
};
class Stream {
  static current: Stream;
  listeners = new Map<string, (event: MessageEvent<string>) => void>();
  constructor() { Stream.current = this; }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) { this.listeners.set(type, listener); }
  close() {}
  emit(type: string, value: unknown) { this.listeners.get(type)?.({ data: JSON.stringify(value) } as MessageEvent<string>); }
}

describe("meeting workbench controls", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal("EventSource", Stream);
    vi.mocked(api.meetings).mockResolvedValue([{ id: "team", file: "team.toml", name: "團隊" }]);
    vi.mocked(api.session).mockResolvedValue(detail);
    vi.mocked(api.startMeeting).mockResolvedValue({ accepted: true, operation: "run", message: "started" });
    vi.mocked(api.diarize).mockResolvedValue({ accepted: true, operation: "diarize", message: "started" });
    vi.mocked(api.stop).mockResolvedValue({ accepted: true, operation: "stop", message: "stop requested" });
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("requires a speaker count, keeps dual disabled and starts the selected real configuration", async () => {
    const changed = vi.fn().mockResolvedValue(undefined);
    render(<MeetingWorkbench runtime={idle} onChanged={changed} devices={{ api_version: 1, current: "default", sources: [{ id: "mic", name: "麥克風" }] }} />);
    await screen.findByRole("option", { name: "團隊" });
    expect(screen.getByRole("button", { name: "開始錄音" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByLabelText("錄音來源模式").hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("option", { name: /v1 不提供/, hidden: true }).hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText("介面預覽")).toBeNull();
    expect(screen.queryByText("LLM總結方式")).toBeNull();
    fireEvent.change(screen.getByLabelText("預計參與人數（必填）"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("錄音裝置（選填）"), { target: { value: "mic" } });
    fireEvent.click(screen.getByRole("button", { name: "開始錄音" }));
    await waitFor(() => expect(api.startMeeting).toHaveBeenCalledExactlyOnceWith({ meeting_id: "team", speakers: 3, source: "mic" }));
    await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  });

  it("imports the staged path with the original display name", async () => {
    vi.mocked(api.uploadAudio).mockResolvedValue({ api_version: 1, name: "original.wav", path: "/tmp/staging/upload.wav", size_bytes: 20 });
    render(<MeetingWorkbench runtime={idle} />);
    await screen.findByRole("option", { name: "團隊" });
    fireEvent.change(screen.getByLabelText("輸入方式"), { target: { value: "file" } });
    fireEvent.change(screen.getByLabelText("預計參與人數（必填）"), { target: { value: "4" } });
    expect(screen.getByRole("button", { name: "開始匯入" }).hasAttribute("disabled")).toBe(true);
    const file = new File(["test"], "original.wav", { type: "audio/wav" });
    fireEvent.change(screen.getByLabelText("會議音檔"), { target: { files: [file] } });
    await screen.findByText("已準備：original.wav");
    fireEvent.click(screen.getByRole("button", { name: "開始匯入" }));
    await waitFor(() => expect(api.startMeeting).toHaveBeenCalledExactlyOnceWith({ meeting_id: "team", speakers: 4, input_file: "/tmp/staging/upload.wav", original_name: "original.wav" }));
  });

  it("creates and updates meeting settings through the real API surface", async () => {
    vi.mocked(api.createMeeting).mockResolvedValue({ api_version: 1, id: "設計", file: "設計.toml", content: "[meeting]" });
    vi.mocked(api.meeting).mockResolvedValue({ api_version: 1, id: "team", file: "team.toml", content: '[meeting]\nname="原設定"' });
    vi.mocked(api.updateMeeting).mockResolvedValue({ api_version: 1, id: "team", file: "team.toml", content: '[meeting]\nname="新設定"' });
    render(<MeetingWorkbench runtime={idle} />);
    fireEvent.change(screen.getByPlaceholderText("例如：系統設計討論"), { target: { value: "設計" } });
    fireEvent.click(screen.getByRole("button", { name: "建立會議設定" }));
    await waitFor(() => expect(api.createMeeting).toHaveBeenCalledWith("設計", '[meeting]\nname = "設計"\n'));
    await screen.findByText("會議設定已建立。");
    fireEvent.click(screen.getByText("編輯會議設定"));
    fireEvent.click(screen.getByRole("button", { name: "載入設定內容" }));
    const editor = await screen.findByLabelText("會議 TOML 設定");
    fireEvent.change(editor, { target: { value: '[meeting]\nname="新設定"' } });
    fireEvent.click(screen.getByRole("button", { name: "儲存會議設定" }));
    await waitFor(() => expect(api.updateMeeting).toHaveBeenCalledWith("team", '[meeting]\nname="新設定"'));
  });

  it("reports startup rejection and never fabricates recording state", async () => {
    vi.mocked(api.startMeeting).mockRejectedValue(new Error("另一項工作已執行"));
    render(<MeetingWorkbench runtime={idle} />);
    await screen.findByRole("option", { name: "團隊" });
    fireEvent.change(screen.getByLabelText("預計參與人數（必填）"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "開始錄音" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toBe("另一項工作已執行");
    expect(screen.queryByRole("region", { name: "目前會議工作" })).toBeNull();
  });

  it("shows the source, explicit empty result and starts then cancels diarization", async () => {
    const { rerender } = render(<MeetingWorkbench runtime={idle} sessions={[summary]} />);
    await screen.findByText("真實原稿");
    expect(screen.getByText(/來源：匯入 · meeting.ogg · 2:00/)).toBeTruthy();
    expect(screen.getByText(/尚未辨識發言者；原逐字稿已保留/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "開始辨識" }));
    await waitFor(() => expect(api.diarize).toHaveBeenCalledExactlyOnceWith(summary.id, 3));
    await screen.findByText("會後辨識已啟動。");
    const running: RuntimeStatus = { schema_version: 2, running: true, pid: 12, work_type: "meeting", session: "/output/meetings/one", mode: "diarize",
      status: { phase: "diarizing", diarization: { stage: "segmentation", processed_seconds: 10, total_seconds: null, requested_speakers: 3, speakers_found: 2 } } };
    rerender(<MeetingWorkbench runtime={running} sessions={[summary]} />);
    const live = screen.getByRole("region", { name: "目前會議工作" });
    expect(within(live).getByText("要求 3 位 · 已找到 2 個代號")).toBeTruthy();
    expect(within(live).queryByRole("progressbar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "正常取消" }));
    await waitFor(() => expect(api.stop).toHaveBeenCalledExactlyOnceWith(false));
    await screen.findByRole("button", { name: "正在停止…" });
    fireEvent.click(screen.getByRole("button", { name: "強制停止" }));
    await waitFor(() => expect(api.stop).toHaveBeenLastCalledWith(true));
    rerender(<MeetingWorkbench runtime={idle} sessions={[{ ...summary, diarization_status: { phase: "aborted", stop_reason: "user" } }]} />);
    await screen.findByRole("button", { name: "重跑辨識" });
    fireEvent.click(screen.getByRole("button", { name: "重跑辨識" }));
    await waitFor(() => expect(api.diarize).toHaveBeenCalledTimes(2));
    await screen.findByText("會後辨識已啟動。");
  });

  it("keeps stop state while hidden, allows force and follows live transcript SSE", async () => {
    const runtime: RuntimeStatus = { schema_version: 2, running: true, pid: 10, work_type: "meeting", mode: "live", session: "/output/meetings/one", status: { phase: "recording" } };
    const { rerender } = render(<MeetingWorkbench runtime={runtime} sessions={[summary]} />);
    await screen.findByText("真實原稿");
    act(() => Stream.current.emit("content", { target: "transcript", operation: "append", content: "持續發言", size_bytes: 30 }));
    await screen.findByText("真實原稿持續發言");
    fireEvent.click(screen.getByRole("button", { name: "正常停止" }));
    await screen.findByRole("button", { name: "正在停止…" });
    rerender(<MeetingWorkbench active={false} runtime={runtime} sessions={[summary]} />);
    rerender(<MeetingWorkbench runtime={runtime} sessions={[summary]} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "強制停止" }).hasAttribute("disabled")).toBe(false));
    expect(api.stop).toHaveBeenCalledExactlyOnceWith(false);
    expect(screen.getByRole("button", { name: "正在停止…" }).getAttribute("aria-busy")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "強制停止" }));
    await waitFor(() => expect(api.stop).toHaveBeenLastCalledWith(true));
    await screen.findByRole("button", { name: "正在中止…" });
  });

  it("renders real 10/4 speakers, collapses short speakers and replaces a generation through SSE", async () => {
    const speakers = Array.from({ length: 10 }, (_, index) => ({ id: `S${String(index + 1).padStart(2, "0")}`, speech_seconds: index < 4 ? 100 : 1, segment_count: 1 }));
    vi.mocked(api.session).mockResolvedValue({ ...detail, speaker_transcript: { content: "S01: 真實發言", size_bytes: 16 }, speakers: { actual_speakers: 10, speakers } });
    render(<MeetingWorkbench runtime={idle} sessions={[summary]} />);
    await screen.findByText("4 位主要發言者");
    expect(screen.queryByText("S05")).toBeNull();
    fireEvent.click(screen.getByText("其他短發言 · 6 位 展開"));
    expect(screen.getByText("S05")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "帶發言者逐字稿" }));
    expect(screen.getByText("S01: 真實發言")).toBeTruthy();
    act(() => Stream.current.emit("content", { target: "speaker_transcript", operation: "replace", content: "S02: 新版本", size_bytes: 15 }));
    await screen.findByText("S02: 新版本");
    expect(screen.queryByText("S01: 真實發言")).toBeNull();
    expect(screen.getByRole("button", { name: "重新辨識" })).toBeTruthy();
  });

  it("never offers another work type for sharing and disables rerun without a source", async () => {
    vi.mocked(api.session).mockResolvedValue({ ...detail, source: undefined, session: { ...summary, has_recording: false } });
    render(<MeetingWorkbench runtime={idle} sessions={[{ ...summary, has_recording: false }, { ...summary, id: "lecture/one", work_type: "lecture" }]} />);
    await screen.findByText("真實原稿");
    expect(screen.getByText("分享目標：meetings/one")).toBeTruthy();
    expect(screen.queryByRole("option", { name: "lecture/one" })).toBeNull();
    expect(screen.getByRole("button", { name: "開始辨識" }).hasAttribute("disabled")).toBe(true);
  });
});
