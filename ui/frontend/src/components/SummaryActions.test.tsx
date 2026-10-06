import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { SummaryActions } from "./SummaryActions";
vi.mock("../api", () => ({ api: { summarize: vi.fn().mockResolvedValue({ message: "started" }) } }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
describe("SummaryActions", () => {
  for (const mode of ["", "all", "section"]) {
    it(`passes saved upstream and redo mode ${mode}`, async () => {
      render(<SummaryActions sessionId="session" disabled={false} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} onMessage={vi.fn()}
        models={{ schema_version: 1, summary: { selected: "missing", models: [] }, whisper: { selected: "", models: [] },
          summary_upstreams: { selected: "local", options: [{ id: "lab", name: "Lab", kind: "api" }] } }} />);
      fireEvent.change(screen.getByLabelText("補做摘要上游"), { target: { value: "lab" } });
      fireEvent.change(screen.getByLabelText("補做或重做"), { target: { value: mode } });
      if (mode === "section") fireEvent.change(screen.getByLabelText("段落時間"), { target: { value: "00:05:00" } });
      fireEvent.click(screen.getByRole("button", { name: "執行課堂摘要" }));
      await waitFor(() => expect(api.summarize).toHaveBeenCalledWith("session", mode === "section" ? "00:05:00" : mode || undefined, "lab"));
    });
  }
});
