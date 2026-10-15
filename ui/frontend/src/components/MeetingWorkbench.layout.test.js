// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";

it("constrains the meeting grid while the history strip scrolls within its card", () => {
  const styles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");
  const workbench = styles.match(/\.meeting-workbench\s*\{([^}]+)\}/)[1];
  const history = styles.match(/\.meeting-history-list\s*\{([^}]+)\}/)[1];
  expect(workbench).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  expect(history).toMatch(/overflow-x:\s*auto/);
});
