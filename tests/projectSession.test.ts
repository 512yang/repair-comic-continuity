import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { readAtomicJson } from "../src/server/atomicJson";
import { loadDraft, saveDraft } from "../src/server/draftStore";
import { createProjectSession } from "../src/server/projectSession";

const temporaryRoots: string[] = [];

function makeProject(): string {
  const root = mkdtempSync(join(tmpdir(), "comic-workbench-"));
  temporaryRoots.push(root);
  mkdirSync(join(root, "输入"));
  mkdirSync(join(root, "输出"));
  mkdirSync(join(root, "人物参考图"));
  writeFileSync(join(root, "输入", "10.jpg"), Buffer.from("page-ten"));
  writeFileSync(join(root, "输入", "2.jpg"), Buffer.from("page-two"));
  writeFileSync(join(root, "小说.txt"), "测试小说", "utf8");
  writeFileSync(join(root, "人物参考图", "主角.png"), Buffer.from("reference"));
  return root;
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("project sessions", () => {
  it("seals naturally ordered input pages and locks mode after submission", () => {
    const root = makeProject();
    const before = readFileSync(join(root, "输入", "2.jpg"));
    const session = createProjectSession(root, "human_visual_auto_text");

    expect(session.inventory.pages.map((page) => page.path)).toEqual([
      "2.jpg",
      "10.jpg",
    ]);
    expect(session.inventory.pages[0]?.sha256).toMatch(/^[a-f0-9]{64}$/);
    session.lockMode("human_visual_auto_text");
    expect(() => session.lockMode("automatic")).toThrow(/mode is already locked/i);
    expect(readFileSync(join(root, "输入", "2.jpg"))).toEqual(before);
  });

  it("autosaves per-page drafts and rejects paths outside the project", () => {
    const root = makeProject();
    saveDraft(root, "input_review", "分镜/2.jpg", {
      state: "annotated",
      note: "肤色不一致",
    });
    expect(loadDraft(root, "input_review", "分镜/2.jpg")).toMatchObject({
      state: "annotated",
      note: "肤色不一致",
    });
    expect(() =>
      saveDraft(root, "input_review", "../2.jpg", { note: "unsafe" }),
    ).toThrow(/safe relative path/i);
  });

  it("recovers one valid sibling temporary JSON after interruption", () => {
    const root = makeProject();
    const target = join(root, ".comic-review-workbench", "recovery.json");
    mkdirSync(join(root, ".comic-review-workbench"), { recursive: true });
    const temporary = join(
      root,
      ".comic-review-workbench",
      ".recovery.json.crash.tmp",
    );
    writeFileSync(temporary, JSON.stringify({ recovered: true }), "utf8");

    expect(readAtomicJson<{ recovered: boolean }>(target)).toEqual({
      recovered: true,
    });
    expect(existsSync(target)).toBe(true);
    expect(existsSync(temporary)).toBe(false);
  });
});
