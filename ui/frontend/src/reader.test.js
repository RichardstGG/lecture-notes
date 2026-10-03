import { readFileSync } from "node:fs";
import { resolve } from "node:path";
const html = readFileSync(resolve(process.cwd(), "../backend/share_static/index.html"), "utf8");
const script = readFileSync(resolve(process.cwd(), "../backend/share_static/reader.js"), "utf8");
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const snapshot = {
  course: "測試課", captured_at: "2026-10-02T12:00:00Z",
  transcript: { content: '<img src="https://external.invalid/tracker">' }, notes: { content: "初步筆記" },
};
const response = (data, status = 200) => new Response(JSON.stringify(data), { status });
async function flush() { for (let i = 0; i < 15; i++) await Promise.resolve(); }

describe("independent reader", () => {
  beforeEach(() => { vi.useFakeTimers(); document.body.innerHTML = html; });
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); document.body.innerHTML = ""; });
  it("requires joining, renders safe text, updates and clears revoked content", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ error: { message: "請加入" } }, 401));
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("location", { hash: "#invite" });
    new Function(script)();
    await flush();
    expect(document.getElementById("reader").hidden).toBe(true);
    document.getElementById("nickname").value = "小明";
    fetch.mockResolvedValueOnce(response({ joined: true })).mockResolvedValueOnce(response(snapshot));
    document.getElementById("join-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush();
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ invitation: "invite", nickname: "小明" });
    expect(document.getElementById("transcript").textContent).toBe(snapshot.transcript.content);
    expect(document.querySelector("img")).toBeNull();
    expect(document.getElementById("reader").hidden).toBe(false);
    fetch.mockResolvedValueOnce(response({ ...snapshot, notes: { content: "更新筆記" } }));
    await vi.advanceTimersByTimeAsync(2000);
    expect(document.getElementById("secondary").textContent).toBe("更新筆記");
    fetch.mockResolvedValueOnce(response({ error: { message: "已關閉" } }, 410));
    await vi.advanceTimersByTimeAsync(2000);
    expect(document.getElementById("reader").hidden).toBe(true);
    expect(document.getElementById("secondary").textContent).toBe("");
    expect(vi.getTimerCount()).toBe(0);
  });
  it("resumes cookies, retries network errors and leaves", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(snapshot));
    vi.stubGlobal("fetch", fetch);
    new Function(script)();
    await flush();
    fetch.mockRejectedValueOnce(new TypeError("network"));
    await vi.advanceTimersByTimeAsync(2000);
    expect(vi.getTimerCount()).toBe(1);
    fetch.mockResolvedValueOnce(response(snapshot));
    await vi.advanceTimersByTimeAsync(2000);
    fetch.mockResolvedValueOnce(response({ left: true }));
    document.getElementById("leave").click();
    await flush();
    expect(document.getElementById("status").textContent).toBe("已離開分享。");
    expect(vi.getTimerCount()).toBe(0);
  });
  it("renders a meeting's raw and speaker transcripts without a notes download", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({
      ...snapshot, work_type: "meeting", course: "設計會議", notes: undefined,
      speaker_transcript: { content: "[00:00:01.000] S01: 大家好" },
    }));
    vi.stubGlobal("fetch", fetch);
    new Function(script)();
    await flush();
    expect(document.getElementById("secondary-heading").textContent).toBe("帶發言者逐字稿");
    expect(document.getElementById("secondary").textContent).toContain("S01");
    expect(document.getElementById("secondary-download").getAttribute("href")).toBe("/share/v1/download/speaker-transcript");
    expect(document.body.textContent).not.toContain("課堂筆記");
  });
});
