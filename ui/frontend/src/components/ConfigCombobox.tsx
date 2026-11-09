import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

interface ConfigOption {
  id: string;
  name?: string;
  error?: string;
}

export function sortConfigsByRecentUse<T extends ConfigOption>(
  items: T[], sessions: { course?: string; started_at?: string | null }[],
): T[] {
  const latest = new Map<string, number>();
  for (const session of sessions) {
    const time = Date.parse(session.started_at || "");
    if (session.course && Number.isFinite(time)) {
      latest.set(session.course, Math.max(latest.get(session.course) ?? -Infinity, time));
    }
  }
  return [...items].sort((a, b) => {
    const aTime = latest.get(a.name || a.id) ?? -Infinity;
    const bTime = latest.get(b.name || b.id) ?? -Infinity;
    return aTime === bTime ? 0 : bTime - aTime;
  });
}

interface Props {
  label: string;
  items: ConfigOption[];
  value: string;
  disabled?: boolean;
  placeholder?: string;
  onSelect: (id: string) => void;
  onCreate: (name: string) => Promise<boolean>;
}

export function ConfigCombobox({ label, items, value, disabled, placeholder, onSelect, onCreate }: Props) {
  const id = useId();
  const [query, setQuery] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const creating = useRef(false);
  const list = useRef<HTMLUListElement>(null);
  const selected = items.find((item) => item.id === value);
  const selectedName = selected?.name || selected?.id || "";
  const text = query?.trim() || "";
  const search = text.toLowerCase();
  const matches = items.filter((item) => !search || [item.name || "", item.id].some((name) => name.toLowerCase().includes(search)));
  const options = matches.length ? matches.map((item) => ({
    id: item.id, label: `${item.name || item.id}${item.error ? "（設定有誤）" : ""}`, disabled: !!item.error,
  })) : text ? [{ id: text, label: `建立「${text}」`, disabled: false }] : [];
  const activeOption = open && !disabled && options[active] && !options[active].disabled ? active : -1;

  useEffect(() => {
    if (activeOption >= 0) list.current?.children[activeOption]?.scrollIntoView?.({ block: "nearest" });
  }, [activeOption]);

  function reset() {
    setQuery(null); setOpen(false); setActive(-1);
  }

  async function choose(index: number) {
    const option = options[index];
    if (disabled || creating.current || !option || option.disabled) return;
    if (matches.length) {
      onSelect(option.id); reset();
    } else {
      creating.current = true;
      try {
        if (await onCreate(text)) reset();
      } finally {
        creating.current = false;
      }
    }
  }

  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    // Enter must never submit the surrounding recording form, including during IME composition.
    if (event.key === "Enter") event.preventDefault();
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === "Escape") {
      event.preventDefault(); reset();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      const direction = event.key === "ArrowDown" ? 1 : -1;
      let next = activeOption < 0 ? (direction === 1 ? -1 : 0) : activeOption;
      for (let count = 0; count < options.length; count++) {
        next = (next + direction + options.length) % options.length;
        if (!options[next].disabled) { setActive(next); break; }
      }
    } else if (event.key === "Enter" && open) {
      void choose(activeOption >= 0 ? activeOption : options.findIndex((option) => !option.disabled));
    }
  }

  return <div className="config-combobox">
    <input role="combobox" aria-label={label} aria-autocomplete="list" aria-expanded={open && !disabled}
      aria-controls={`${id}-list`} aria-activedescendant={activeOption >= 0 ? `${id}-${activeOption}` : undefined}
      autoComplete="off" disabled={disabled} placeholder={placeholder} value={query ?? selectedName}
      onFocus={() => { setOpen(true); setActive(-1); }}
      onClick={() => setOpen(true)}
      onChange={(event) => { setQuery(event.target.value); setOpen(true); setActive(-1); }}
      onBlur={() => { setOpen(false); if (!creating.current) reset(); }} onKeyDown={keyDown} />
    {open && !disabled && <ul ref={list} id={`${id}-list`} role="listbox" aria-label={label} className="config-combobox-list">
      {options.map((option, index) => <li id={`${id}-${index}`} key={option.id} role="option"
        aria-selected={matches.length > 0 && option.id === value} aria-disabled={option.disabled}
        className={activeOption === index ? "active" : undefined}
        onMouseDown={(event) => event.preventDefault()} onClick={(event) => { event.preventDefault(); void choose(index); }}>{option.label}</li>)}
    </ul>}
  </div>;
}
