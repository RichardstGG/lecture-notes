import { useState } from "react";
import { act, cleanup, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfigCombobox, sortConfigsByRecentUse } from "./ConfigCombobox";

const items = [
  { id: "cs-101", name: "Algorithms" },
  { id: "broken", name: "Broken", error: "invalid" },
  { id: "weekly", name: "團隊週會" },
];

function setup() {
  const onSelect = vi.fn();
  const onCreate = vi.fn().mockResolvedValue(true);
  const submit = vi.fn((event) => event.preventDefault());
  function Form() {
    const [value, setValue] = useState("cs-101");
    return <form onSubmit={submit}><label>設定<ConfigCombobox label="設定" items={items} value={value}
      onSelect={(id) => { onSelect(id); setValue(id); }} onCreate={onCreate} /></label><button>開始</button></form>;
  }
  render(<Form />);
  const input = screen.getByRole("combobox", { name: "設定" }) as HTMLInputElement;
  fireEvent.focus(input);
  return { input, onSelect, onCreate, submit };
}

afterEach(cleanup);

describe("sortConfigsByRecentUse", () => {
  it("matches display names or fallback ids, compares parsed dates and keeps ties stable without mutating items", () => {
    const configs = [
      { id: "unused", name: "Unused" }, { id: "a", name: "Alpha" },
      { id: "fallback", name: "" }, { id: "b", name: "Beta" }, { id: "pre-epoch" },
    ];
    const original = [...configs];
    const sorted = sortConfigsByRecentUse(configs, [
      { course: "Alpha", started_at: "2026-11-09T12:00:00+08:00" },
      { course: "Alpha", started_at: "2026-11-01T00:00:00Z" },
      { course: "fallback", started_at: "2026-11-09T05:00:00Z" },
      { course: "Beta", started_at: "2026-11-09T04:00:00Z" },
      { course: "unused", started_at: "2099-01-01T00:00:00Z" },
      { course: "pre-epoch", started_at: "1960-01-01T00:00:00Z" },
      { started_at: "2099-01-01T00:00:00Z" },
    ]);
    expect(sorted.map((item) => item.id)).toEqual(["fallback", "a", "b", "pre-epoch", "unused"]);
    expect(configs).toEqual(original);
    expect(sortConfigsByRecentUse(configs, [])).toEqual(original);
  });

  it.each(["", undefined, null, "not-a-date"])("treats started_at %s as unused without losing valid usage", (started_at) => {
    const configs = [{ id: "unused" }, { id: "invalid" }, { id: "used" }];
    expect(sortConfigsByRecentUse(configs, [
      { course: "invalid", started_at },
      { course: "used", started_at: "2026-11-09T00:00:00Z" },
      { course: "used", started_at },
    ]).map((item) => item.id)).toEqual(["used", "unused", "invalid"]);
  });
});

describe("ConfigCombobox", () => {
  it("lists all configurations on focus and filters names and ids without case sensitivity", () => {
    const { input } = setup();
    expect(input.value).toBe("Algorithms");
    expect(screen.getAllByRole("option")).toHaveLength(3);
    expect(input.getAttribute("aria-controls")).toBe(screen.getByRole("listbox").id);
    for (const query of ["ALGO", "CS-1", "週會"]) {
      fireEvent.change(input, { target: { value: query } });
      expect(screen.getAllByRole("option")).toHaveLength(1);
      expect(screen.queryByRole("option", { name: /建立/ })).toBeNull();
    }
    fireEvent.change(input, { target: { value: "BROKEN" } });
    expect(screen.getByRole("option", { name: "Broken（設定有誤）" }).getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(screen.getByRole("option"));
    expect(input.value).toBe("BROKEN");
  });

  it("offers only a trimmed creation name for no matches, never for whitespace", async () => {
    const { input, onCreate, onSelect } = setup();
    fireEvent.change(input, { target: { value: "  新設定  " } });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    fireEvent.click(screen.getByRole("option", { name: "建立「新設定」" }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledExactlyOnceWith("新設定"));
    expect(onSelect).not.toHaveBeenCalled();
    await waitFor(() => expect(input.getAttribute("aria-expanded")).toBe("false"));
    fireEvent.change(input, { target: { value: "   " } });
    expect(screen.queryByRole("option", { name: /建立/ })).toBeNull();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCreate).toHaveBeenCalledOnce();
  });

  it("restores the actual selection on blur or Escape and allows reopening after selection", () => {
    const { input, onSelect } = setup();
    fireEvent.change(input, { target: { value: "unused draft" } });
    fireEvent.blur(input);
    expect(input.value).toBe("Algorithms");
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "another draft" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("Algorithms");
    fireEvent.click(input);
    fireEvent.click(screen.getByRole("option", { name: "團隊週會" }));
    expect(onSelect).toHaveBeenCalledWith("weekly");
    expect(input.value).toBe("團隊週會");
    fireEvent.click(input);
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });

  it("moves in both directions, wraps, skips disabled options and selects with Enter", () => {
    const { input, onSelect } = setup();
    const active = () => document.getElementById(input.getAttribute("aria-activedescendant") || "")?.textContent;
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(active()).toBe("Algorithms");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(active()).toBe("團隊週會");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(active()).toBe("Algorithms");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(active()).toBe("團隊週會");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(active()).toBe("Algorithms");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("weekly");
    expect(input.value).toBe("團隊週會");
  });

  it.each([{ isComposing: true }, { keyCode: 229 }])("ignores IME Enter (%j) for selection and creation while preventing form submit", (composition) => {
    const { input, onSelect, onCreate, submit } = setup();
    for (const query of ["CS-1", "new config"]) {
      fireEvent.change(input, { target: { value: query } });
      const event = createEvent.keyDown(input, { key: "Enter", cancelable: true, ...composition });
      fireEvent(input, event);
      expect(event.defaultPrevented).toBe(true);
      expect(input.value).toBe(query);
    }
    expect(onSelect).not.toHaveBeenCalled();
    expect(onCreate).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("always cancels Enter, including closed, empty and disabled-result lists", () => {
    const { input, onCreate, submit } = setup();
    for (const query of ["", "broken", "missing"]) {
      fireEvent.change(input, { target: { value: query } });
      if (query === "missing") fireEvent.keyDown(input, { key: "Escape" });
      const event = createEvent.keyDown(input, { key: "Enter", cancelable: true });
      fireEvent(input, event);
      expect(event.defaultPrevented).toBe(true);
    }
    expect(onCreate).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("keeps rejected creation text even when pending blurs the input, then resets on Escape", async () => {
    const { input, onCreate } = setup();
    let finish!: (value: boolean) => void;
    onCreate.mockReturnValue(new Promise<boolean>((resolve) => { finish = resolve; }));
    fireEvent.change(input, { target: { value: " invalid/name " } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.blur(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCreate).toHaveBeenCalledExactlyOnceWith("invalid/name");
    await act(async () => finish(false));
    expect(input.value).toBe(" invalid/name ");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("Algorithms");
  });

  it("does not offer to create when an empty inventory receives only whitespace", () => {
    const onCreate = vi.fn();
    render(<ConfigCombobox label="空設定" items={[]} value="" onSelect={vi.fn()} onCreate={onCreate} />);
    const input = screen.getByRole("combobox");
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "   " } });
    expect(screen.queryByRole("option")).toBeNull();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    const event = createEvent.keyDown(input, { key: "Enter", cancelable: true });
    fireEvent(input, event);
    expect(event.defaultPrevented).toBe(true);
    expect(onCreate).not.toHaveBeenCalled();
  });
});
