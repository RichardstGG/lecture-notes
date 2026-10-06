import type { MouseEvent } from "react";
import { Icon } from "./Icon";

export type Page = "lecture" | "meeting" | "local-settings";
export interface Navigation { page: Page; target: string }

const groups = [
  { page: "lecture" as const, label: "課堂工作台", target: "live", icon: "wave" as const,
    children: [{ label: "歷史紀錄", target: "history" }, { label: "內網共享", target: "lecture-sharing" }, { label: "課程設定", target: "courses" }] },
  { page: "meeting" as const, label: "會議工作台", target: "meeting-live", icon: "mic" as const,
    children: [{ label: "歷史紀錄", target: "meeting-history" }, { label: "內網共享", target: "meeting-sharing" }, { label: "會議設定", target: "meeting-settings" }] },
];

export function navigationFromHash(hash: string): Navigation {
  const target = hash.replace(/^#/, "");
  if (target === "local-settings") return { page: "local-settings", target };
  for (const group of groups) {
    if (target === group.target || group.children.some((child) => child.target === target)) return { page: group.page, target };
  }
  return { page: "lecture", target: "live" };
}

export function WorkbenchNavigation({ navigation, onNavigate }: {
  navigation: Navigation; onNavigate: (next: Navigation) => void;
}) {
  function follow(event: MouseEvent<HTMLAnchorElement>, next: Navigation) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onNavigate(next);
  }
  return <nav aria-label="主要導航">
    {groups.map((group) => <div className="nav-group" key={group.page} role="group" aria-label={`${group.label}選單`}>
      <a href={`#${group.target}`} className={navigation.page === group.page ? "active" : ""}
        aria-current={navigation.page === group.page ? "page" : undefined}
        onClick={(event) => follow(event, { page: group.page, target: group.target })}>
        <Icon name={group.icon} />{group.label}
      </a>
      <div className="nav-children">{group.children.map((child) => <a href={`#${child.target}`} key={child.target}
        className={navigation.target === child.target ? "active" : ""}
        aria-current={navigation.target === child.target ? "location" : undefined}
        onClick={(event) => follow(event, { page: group.page, target: child.target })}>{child.label}</a>)}</div>
    </div>)}
    <a href="#local-settings" className={navigation.page === "local-settings" ? "active" : ""}
      aria-current={navigation.page === "local-settings" ? "page" : undefined}
      onClick={(event) => follow(event, { page: "local-settings", target: "local-settings" })}><Icon name="mic" />本機設定</a>
  </nav>;
}
