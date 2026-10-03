// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MeetingWorkbench } from "./MeetingWorkbench";

vi.mock("./SharePanel", () => ({ SharePanel: ({ selectedId }: { selectedId?: string }) => <div>分享目標：{selectedId || "無"}</div> }));

describe("meeting workbench preview", () => {
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
});
