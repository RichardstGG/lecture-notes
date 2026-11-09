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
  close = vi.fn();
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
    await waitFor(() => expect((screen.getByRole("combobox", { name: "會議設定" }) as HTMLInputElement).value).toBe("團隊"));
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
    await waitFor(() => expect((screen.getByRole("combobox", { name: "會議設定" }) as HTMLInputElement).value).toBe("團隊"));
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
    vi.mocked(api.meetings).mockResolvedValueOnce([{ id: "team", file: "team.toml", name: "團隊" }])
      .mockResolvedValue([{ id: "team", file: "team.toml", name: "團隊" }, { id: "設計", file: "設計.toml", name: "設計" }]);
    vi.mocked(api.meeting).mockResolvedValue({ api_version: 1, id: "設計", file: "設計.toml", content: '[meeting]\nname="原設定"' });
    vi.mocked(api.updateMeeting).mockResolvedValue({ api_version: 1, id: "設計", file: "設計.toml", content: '[meeting]\nname="新設定"' });
    render(<MeetingWorkbench view="meeting-settings" runtime={idle} />);
    fireEvent.change(screen.getByPlaceholderText("例如：系統設計討論"), { target: { value: "設計" } });
    fireEvent.click(screen.getByRole("button", { name: "建立會議設定" }));
    await waitFor(() => expect(api.createMeeting).toHaveBeenCalledWith("設計", '[meeting]\nname = "設計"\n'));
    await screen.findByText("會議設定已建立。");
    fireEvent.click(screen.getByRole("button", { name: "載入設定內容" }));
    const editor = await screen.findByLabelText("會議 TOML 設定");
    expect(api.meeting).toHaveBeenCalledWith("設計");
    fireEvent.change(editor, { target: { value: '[meeting]\nname="新設定"' } });
    fireEvent.click(screen.getByRole("button", { name: "儲存會議設定" }));
    await waitFor(() => expect(api.updateMeeting).toHaveBeenCalledWith("設計", '[meeting]\nname="新設定"'));
  });

  it.each([
    ["meeting-live", "會議設定"], ["meeting-settings", "要編輯的會議設定"],
  ])("filters, selects and creates from %s without starting recording", async (view, label) => {
    const configs = [{ id: "team", file: "team.toml", name: "團隊" }, { id: "DESIGN", file: "design.toml", name: "Design Review" },
      { id: "broken", file: "broken.toml", name: "壞設定", error: "invalid" }];
    vi.mocked(api.meetings).mockResolvedValue(configs);
    let finish!: (value: Awaited<ReturnType<typeof api.createMeeting>>) => void;
    vi.mocked(api.createMeeting).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const { rerender } = render(<MeetingWorkbench view={view} runtime={idle} />);
    const input = screen.getByRole("combobox", { name: label }) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("團隊"));
    fireEvent.focus(input);
    expect(screen.getAllByRole("option").filter((option) => option.closest('[role="listbox"]'))).toHaveLength(3);
    for (const query of ["design", "review"]) {
      fireEvent.change(input, { target: { value: query } });
      expect(screen.getByRole("option", { name: "Design Review" })).toBeTruthy();
      expect(screen.queryByRole("option", { name: /建立/ })).toBeNull();
    }
    fireEvent.click(screen.getByRole("option", { name: "Design Review" }));
    expect(input.value).toBe("Design Review");
    fireEvent.change(input, { target: { value: "BROKEN" } });
    expect(screen.getByRole("option", { name: "壞設定（設定有誤）" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.queryByRole("option", { name: /建立/ })).toBeNull();
    fireEvent.blur(input);
    expect(input.value).toBe("Design Review");
    fireEvent.change(input, { target: { value: "  新會議  " } });
    fireEvent.click(screen.getByRole("option", { name: "建立「新會議」" }));
    expect(api.createMeeting).toHaveBeenCalledExactlyOnceWith("新會議", '[meeting]\nname = "新會議"\n');
    rerender(<MeetingWorkbench view="meeting-live" runtime={idle} />);
    fireEvent.change(screen.getByLabelText("預計參與人數（必填）"), { target: { value: "3" } });
    expect(screen.getByRole("button", { name: "開始錄音" }).hasAttribute("disabled")).toBe(true);
    fireEvent.submit(screen.getByRole("button", { name: "開始錄音" }).closest("form")!);
    expect(api.startMeeting).not.toHaveBeenCalled();
    vi.mocked(api.meetings).mockResolvedValue([...configs, { id: "new-id", file: "new.toml", name: "新會議" }]);
    await act(async () => finish({ api_version: 1, id: "new-id", file: "new.toml", content: "[meeting]" }));
    await waitFor(() => expect((screen.getByRole("combobox", { name: "會議設定" }) as HTMLInputElement).value).toBe("新會議"));
    expect(api.meetings).toHaveBeenCalledTimes(2);
    expect(api.startMeeting).not.toHaveBeenCalled();
    rerender(<MeetingWorkbench view="meeting-settings" runtime={idle} />);
    expect((screen.getByRole("combobox", { name: "要編輯的會議設定" }) as HTMLInputElement).value).toBe("新會議");
    rerender(<MeetingWorkbench view="meeting-live" runtime={idle} />);
    fireEvent.click(screen.getByRole("button", { name: "開始錄音" }));
    await waitFor(() => expect(api.startMeeting).toHaveBeenCalledExactlyOnceWith({ meeting_id: "new-id", speakers: 3 }));
  });

  it.each([
    ["meeting-live", "會議設定", "名稱不合法"], ["meeting-settings", "要編輯的會議設定", "設定已存在"],
  ])("preserves rejected creation text on %s and prevents IME Enter from acting", async (view, label, message) => {
    vi.mocked(api.createMeeting).mockRejectedValue(new Error(message));
    render(<MeetingWorkbench view={view} runtime={idle} />);
    const input = screen.getByRole("combobox", { name: label }) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("團隊"));
    fireEvent.change(screen.getByLabelText("預計參與人數（必填）"), { target: { value: "3" } });
    for (const query of ["team", " invalid/name "]) {
      fireEvent.change(input, { target: { value: query } });
      fireEvent.keyDown(input, { key: "Enter", isComposing: true });
      fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
      expect(input.value).toBe(query);
    }
    expect(api.createMeeting).not.toHaveBeenCalled();
    expect(api.startMeeting).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect((await screen.findByRole("alert")).textContent).toBe(message);
    expect(input.value).toBe(" invalid/name ");
    expect(api.startMeeting).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("團隊");
  });

  it("shares configuration selection with recording and preserves TOML drafts across subpages", async () => {
    vi.mocked(api.meetings).mockResolvedValue([{ id: "team", file: "team.toml", name: "團隊" }, { id: "design", file: "design.toml", name: "設計" }]);
    vi.mocked(api.meeting).mockImplementation(async (id) => ({ api_version: 1, id, file: `${id}.toml`, content: `[meeting]\nname="${id}"` }));
    vi.mocked(api.updateMeeting).mockResolvedValue({ api_version: 1, id: "design", file: "design.toml", content: '[meeting]\nname="已編輯"' });
    const { rerender } = render(<MeetingWorkbench view="meeting-settings" runtime={idle} />);
    await waitFor(() => expect((screen.getByRole("combobox", { name: "要編輯的會議設定" }) as HTMLInputElement).value).toBe("團隊"));
    expect(screen.queryByRole("button", { name: "開始錄音" })).toBeNull();
    expect(document.querySelector("#meeting-settings details")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "載入設定內容" }));
    await waitFor(() => expect((screen.getByLabelText("會議 TOML 設定") as HTMLTextAreaElement).value).toContain('name="team"'));
    fireEvent.change(screen.getByLabelText("要編輯的會議設定"), { target: { value: "design" } });
    fireEvent.click(screen.getByRole("option", { name: "設計" }));
    expect(screen.queryByLabelText("會議 TOML 設定")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "載入設定內容" }));
    await waitFor(() => expect((screen.getByLabelText("會議 TOML 設定") as HTMLTextAreaElement).value).toContain('name="design"'));
    fireEvent.change(screen.getByLabelText("會議 TOML 設定"), { target: { value: '[meeting]\nname="已編輯"' } });
    rerender(<MeetingWorkbench view="meeting-live" runtime={idle} />);
    expect((screen.getByLabelText("會議設定") as HTMLInputElement).value).toBe("設計");
    expect(screen.queryByRole("button", { name: "儲存會議設定" })).toBeNull();
    rerender(<MeetingWorkbench view="meeting-sharing" runtime={idle} />);
    expect(screen.getByRole("heading", { name: "分享會議逐字稿" })).toBeTruthy();
    rerender(<MeetingWorkbench view="meeting-settings" runtime={idle} />);
    expect((screen.getByLabelText("會議 TOML 設定") as HTMLTextAreaElement).value).toBe('[meeting]\nname="已編輯"');
    fireEvent.click(screen.getByRole("button", { name: "儲存會議設定" }));
    await waitFor(() => expect(api.updateMeeting).toHaveBeenCalledExactlyOnceWith("design", '[meeting]\nname="已編輯"'));
    await screen.findByText("會議設定已儲存。");
    expect(api.startMeeting).not.toHaveBeenCalled();
    expect(api.stop).not.toHaveBeenCalled();
  });

  it("reports startup rejection and never fabricates recording state", async () => {
    vi.mocked(api.startMeeting).mockRejectedValue(new Error("另一項工作已執行"));
    render(<MeetingWorkbench runtime={idle} />);
    await waitFor(() => expect((screen.getByRole("combobox", { name: "會議設定" }) as HTMLInputElement).value).toBe("團隊"));
    fireEvent.change(screen.getByLabelText("預計參與人數（必填）"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "開始錄音" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toBe("另一項工作已執行");
    expect(screen.queryByRole("region", { name: "目前會議工作" })).toBeNull();
  });

  it("shows the source, explicit empty result and starts then cancels diarization", async () => {
    const { rerender } = render(<MeetingWorkbench view="meeting-history" runtime={idle} sessions={[summary]} />);
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
    const retranscribing: RuntimeStatus = { ...running, status: { phase: "diarizing", diarization: { stage: "retranscription", processed_seconds: 60, total_seconds: 120, requested_speakers: 3, speakers_found: 2 } } };
    rerender(<MeetingWorkbench runtime={retranscribing} sessions={[summary]} />);
    expect(within(live).getByRole("progressbar", { name: "會議辨識進度" }).getAttribute("aria-valuenow")).toBe("50");
    rerender(<MeetingWorkbench view="meeting-history" runtime={retranscribing} sessions={[summary]} />);
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("button", { name: "正常取消" })).toBeNull();
    rerender(<MeetingWorkbench runtime={retranscribing} sessions={[summary]} />);
    fireEvent.click(screen.getByRole("button", { name: "正常取消" }));
    await waitFor(() => expect(api.stop).toHaveBeenCalledExactlyOnceWith(false));
    await screen.findByRole("button", { name: "正在停止…" });
    fireEvent.click(screen.getByRole("button", { name: "強制停止" }));
    await waitFor(() => expect(api.stop).toHaveBeenLastCalledWith(true));
    rerender(<MeetingWorkbench view="meeting-history" runtime={idle} sessions={[{ ...summary, diarization_status: { phase: "aborted", stop_reason: "user" } }]} />);
    await screen.findByRole("button", { name: "重跑辨識" });
    fireEvent.click(screen.getByRole("button", { name: "重跑辨識" }));
    await waitFor(() => expect(api.diarize).toHaveBeenCalledTimes(2));
    await screen.findByText("會後辨識已啟動。");
  });

  it("keeps stop state while hidden, allows force and follows live transcript SSE", async () => {
    const runtime: RuntimeStatus = { schema_version: 2, running: true, pid: 10, work_type: "meeting", mode: "live", session: "/output/meetings/one", status: { phase: "recording" } };
    const { rerender } = render(<MeetingWorkbench runtime={runtime} sessions={[summary]} />);
    const live = screen.getByRole("region", { name: "目前會議工作" });
    await within(live).findByText("真實原稿");
    expect(document.getElementById("meeting-history")!.hasAttribute("hidden")).toBe(true);
    expect(screen.queryByRole("button", { name: "開始辨識" })).toBeNull();
    const stream = Stream.current;
    act(() => Stream.current.emit("content", { target: "transcript", operation: "append", content: "持續發言", size_bytes: 30 }));
    await within(live).findByText("真實原稿持續發言");
    fireEvent.click(screen.getByRole("button", { name: "正常停止" }));
    await screen.findByRole("button", { name: "正在停止…" });
    rerender(<MeetingWorkbench view="meeting-history" runtime={runtime} sessions={[summary]} />);
    expect(live.hasAttribute("hidden")).toBe(true);
    expect(screen.queryByRole("button", { name: "強制停止" })).toBeNull();
    expect(screen.getByRole("button", { name: "開始辨識" }).hasAttribute("disabled")).toBe(true);
    rerender(<MeetingWorkbench view="meeting-sharing" runtime={runtime} sessions={[summary]} />);
    act(() => stream.emit("content", { target: "transcript", operation: "append", content: "跨頁更新", size_bytes: 45 }));
    rerender(<MeetingWorkbench runtime={runtime} sessions={[summary]} />);
    expect(Stream.current).toBe(stream);
    expect(stream.close).not.toHaveBeenCalled();
    await within(live).findByText("真實原稿持續發言跨頁更新");
    expect(api.startMeeting).not.toHaveBeenCalled();
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
    render(<MeetingWorkbench view="meeting-history" runtime={idle} sessions={[summary]} />);
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
    const sessions = [{ ...summary, has_recording: false }, { ...summary, id: "lecture/one", course: "課堂紀錄", work_type: "lecture" }];
    const { rerender } = render(<MeetingWorkbench view="meeting-history" runtime={idle} sessions={sessions} />);
    await screen.findByText("真實原稿");
    expect(screen.getByRole("button", { name: "開始辨識" }).hasAttribute("disabled")).toBe(true);
    rerender(<MeetingWorkbench view="meeting-sharing" runtime={idle} sessions={sessions} />);
    expect(screen.getByText("分享目標：meetings/one")).toBeTruthy();
    expect(screen.getByRole("option", { name: "設計會議" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "課堂紀錄" })).toBeNull();
  });

  it("offers the current session when returning to live after viewing an older meeting", async () => {
    const old = { ...summary, id: "meetings/old", course: "舊會議" };
    const sessions = [summary, old];
    const runtime: RuntimeStatus = { schema_version: 2, running: true, work_type: "meeting", session: "/output/meetings/one", status: { phase: "recording" } };
    vi.mocked(api.session).mockImplementation(async (id) => id === old.id ? { ...detail, session: old, transcript: { content: "舊會議原稿", size_bytes: 15 } } : detail);
    const { rerender } = render(<MeetingWorkbench view="meeting-history" runtime={runtime} sessions={sessions} />);
    await within(document.getElementById("meeting-history")!).findByText("真實原稿");
    fireEvent.click(screen.getByRole("button", { name: /舊會議/ }));
    await screen.findByText("舊會議原稿");
    rerender(<MeetingWorkbench runtime={runtime} sessions={sessions} />);
    const live = screen.getByRole("region", { name: "目前會議工作" });
    expect(within(live).queryByText("舊會議原稿")).toBeNull();
    fireEvent.click(within(live).getByRole("button", { name: "查看目前場次" }));
    await within(live).findByText("真實原稿");
    expect(api.startMeeting).not.toHaveBeenCalled();
    expect(api.stop).not.toHaveBeenCalled();
  });
});
