import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { api } from "./api";
import type { SessionSummary } from "./types";

vi.mock("./api", () => ({ api: {
  status: vi.fn(), courses: vi.fn(), models: vi.fn(), sessions: vi.fn(), session: vi.fn(),
  devices: vi.fn(), course: vi.fn(), termCandidates: vi.fn(), sharing: vi.fn(), sharingNetwork: vi.fn(),
  start: vi.fn(), stop: vi.fn(),
} }));

const oldLecture: SessionSummary = { id: "lecture/old", course: "原有課程", started_at: "", updated_at: "", has_transcript: true, has_notes: true, has_recording: false };
const meeting: SessionSummary = { ...oldLecture, id: "meetings/one", course: "真實會議", work_type: "meeting", has_notes: false };
const scroll = vi.fn();

describe("workbench navigation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
    HTMLElement.prototype.scrollIntoView = scroll;
    window.scrollTo = vi.fn();
    vi.stubGlobal("EventSource", class { addEventListener() {} close() {} });
    vi.mocked(api.status).mockResolvedValue({ schema_version: 1, running: false });
    vi.mocked(api.courses).mockResolvedValue([{ id: "course", file: "course.toml", name: "課程", model: "local" }]);
    vi.mocked(api.models).mockResolvedValue({ schema_version: 1, summary: { selected: "local", models: [] }, whisper: { selected: "whisper", models: [] } });
    vi.mocked(api.devices).mockResolvedValue({ api_version: 1, current: "default", sources: [] });
    vi.mocked(api.sessions).mockResolvedValue([meeting, oldLecture]);
    vi.mocked(api.session).mockResolvedValue({ api_version: 1, session: oldLecture, transcript: { content: "舊逐字稿", size_bytes: 12 }, notes: { content: "舊筆記", size_bytes: 9 } });
    vi.mocked(api.course).mockResolvedValue({ api_version: 1, id: "course", file: "course.toml", content: "[course]", vocabulary: { terms: [], glossary: [] } });
    vi.mocked(api.termCandidates).mockResolvedValue({ schema_version: 1, session: oldLecture.id, course: "原有課程", course_id: "course", course_file: "course.toml", whisper_prompt_base: "", defined: { terms: 0, glossary: 0 }, candidates: [] });
    vi.mocked(api.sharing).mockResolvedValue({ api_version: 1, active: false, participants: [] });
    vi.mocked(api.sharingNetwork).mockResolvedValue({ api_version: 1, advertise_host: "192.168.1.2", interface: "eth0" });
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("keeps legacy lectures in the original document/history grid before sharing and courses", async () => {
    const { container } = render(<App />);
    await screen.findByText("舊逐字稿");
    expect(api.session).toHaveBeenCalledWith(oldLecture.id);
    expect(api.termCandidates).not.toHaveBeenCalledWith(meeting.id);
    expect(within(document.getElementById("history")!).queryByText("真實會議")).toBeNull();
    const grid = container.querySelector(".workspace-grid")!;
    expect(Boolean(grid.compareDocumentPosition(document.getElementById("lecture-sharing")!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    expect(Boolean(document.getElementById("lecture-sharing")!.compareDocumentPosition(document.getElementById("courses")!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    expect(screen.getByRole("button", { name: "課堂筆記" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /術語候選/ })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "本機裝置與診斷" })).toBeNull();
  });

  it("switches owner pages before scrolling and preserves lecture and meeting form state", async () => {
    const { container } = render(<App />);
    await screen.findByText("舊逐字稿");
    fireEvent.change(screen.getByRole("combobox", { name: "總結方式" }), { target: { value: "none" } });
    const meetingMenu = screen.getByRole("group", { name: "會議工作台選單" });
    fireEvent.click(within(meetingMenu).getByRole("link", { name: "會議設定" }));
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("會議工作台");
    expect(container.querySelector(".meeting-theme")).toBeTruthy();
    expect(window.location.hash).toBe("#meeting-settings");
    expect(scroll.mock.instances.at(-1)).toBe(document.getElementById("meeting-settings"));
    fireEvent.change(screen.getByPlaceholderText("例如：系統設計討論"), { target: { value: "保留的會議名稱" } });
    fireEvent.click(screen.getByRole("link", { name: "本機設定" }));
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("本機設定");
    expect(screen.getByRole("heading", { name: "本機裝置與診斷" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "逐字稿" })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: /^課堂工作台$/ }));
    expect((screen.getByRole("combobox", { name: "總結方式" }) as HTMLSelectElement).value).toBe("none");
    expect(container.querySelector(".meeting-theme")).toBeNull();
    fireEvent.click(within(meetingMenu).getByRole("link", { name: "內網共享" }));
    expect(scroll.mock.instances.at(-1)).toBe(document.getElementById("meeting-sharing"));
    expect((screen.getByPlaceholderText("例如：系統設計討論") as HTMLInputElement).value).toBe("保留的會議名稱");
    expect(api.start).not.toHaveBeenCalled();
    expect(api.stop).not.toHaveBeenCalled();
  });

  it("restores deep links and browser history to the matching page", async () => {
    window.history.replaceState(null, "", "#local-settings");
    render(<App />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("本機設定");
    await act(async () => {
      window.history.replaceState(null, "", "#meeting-history");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.getByRole("heading", { name: "會議結果與歷史" })).toBeTruthy();
    expect(scroll.mock.instances.at(-1)).toBe(document.getElementById("meeting-history"));
    await act(async () => {
      window.history.replaceState(null, "", "#history");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("課堂工作台"));
    expect(scroll.mock.instances.at(-1)).toBe(document.getElementById("history"));
  });

  it("keeps stop feedback and the background-work banner across pages without stopping twice", async () => {
    vi.mocked(api.status).mockResolvedValue({ schema_version: 1, running: true, work_type: "lecture", course: "原有課程", status: { phase: "finishing" } });
    vi.mocked(api.stop).mockResolvedValue({ accepted: true, operation: "stop", message: "stop requested" });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "正常停止" }));
    await waitFor(() => expect(api.stop).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByRole("button", { name: "立即停止" }).hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByRole("link", { name: "本機設定" }));
    expect(screen.getByText(/背景工作進行中：課堂 · 原有課程 · 收尾中/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "回到工作台" }));
    expect(screen.getByRole("button", { name: "正在停止…" }).getAttribute("aria-busy")).toBe("true");
    expect(api.stop).toHaveBeenCalledExactlyOnceWith(false);
    expect(api.start).not.toHaveBeenCalled();
  });

  it("does not select a meeting as a lecture when there are no lecture sessions", async () => {
    vi.mocked(api.sessions).mockResolvedValue([meeting]);
    render(<App />);
    await screen.findByText("還沒有課堂紀錄。");
    expect(api.session).not.toHaveBeenCalled();
    expect(api.termCandidates).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "開啟分享" }).hasAttribute("disabled")).toBe(true);
  });

  it("keeps meeting diarization progress visible across pages", async () => {
    vi.mocked(api.status).mockResolvedValue({ schema_version: 2, running: true, work_type: "meeting", course: "設計會議", mode: "diarize",
      status: { phase: "diarizing", diarization: { stage: "retranscription", processed_seconds: 600, total_seconds: 1200, requested_speakers: 10, speakers_found: 4 } },
    });
    render(<App />);
    await screen.findByText(/背景工作進行中：會議/);
    fireEvent.click(screen.getByRole("link", { name: "本機設定" }));
    expect(screen.getByText(/重新轉錄 10:00 \/ 20:00/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "回到工作台" }));
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("會議工作台");
    expect(api.stop).not.toHaveBeenCalled();
  });

  it("keeps explicit unknown work types out of both workbenches", async () => {
    vi.mocked(api.sessions).mockResolvedValue([{ ...meeting, id: "other/one", work_type: "future-type" }]);
    vi.mocked(api.status).mockResolvedValue({ schema_version: 2, running: true, work_type: "future-type", status: { phase: "processing" } });
    render(<App />);
    await screen.findByText("還沒有課堂紀錄。");
    expect(screen.getByText(/背景工作進行中：未知類型/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "回到工作台" })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: /^會議工作台$/ }));
    expect(screen.getByText(/尚無會議紀錄/)).toBeTruthy();
    expect(api.session).not.toHaveBeenCalled();
  });
});
