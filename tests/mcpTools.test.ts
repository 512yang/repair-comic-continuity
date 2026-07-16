import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  TOOL_NAMES,
  createWorkbenchToolService,
} from "../src/server/tools";

const roots: string[] = [];

function makeProject(): string {
  const root = mkdtempSync(join(tmpdir(), "mcp-comic-workbench-"));
  roots.push(root);
  mkdirSync(join(root, "输入"));
  mkdirSync(join(root, "输出"));
  writeFileSync(join(root, "输入", "0001.jpg"), "page-one");
  return root;
}

afterEach(() => {
  vi.restoreAllMocks();
  for (const root of roots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("workbench MCP tools", () => {
  it("exposes only the six bounded workbench tools", () => {
    const service = createWorkbenchToolService();
    expect(service.listToolNames()).toEqual(TOOL_NAMES);
    expect(TOOL_NAMES).toEqual([
      "open_comic_review_workbench",
      "save_review_draft",
      "submit_review_drafts",
      "commit_normalized_annotations",
      "get_comic_review_status",
      "record_output_page_decision",
    ]);
  });

  it("returns raw user drafts without inventing structured repair fields", async () => {
    const root = makeProject();
    const service = createWorkbenchToolService();
    await service.callTool("open_comic_review_workbench", {
      projectRoot: root,
      mode: "human_visual_auto_text",
    });
    await service.callTool("save_review_draft", {
      projectRoot: root,
      phase: "input_review",
      page: "0001.jpg",
      draft: {
        state: "annotated",
        note: "男人肤色与前页不一致",
        shapes: [{ shape_kind: "full_page", bbox_norm: [0, 0, 1, 1] }],
      },
    });

    const result = await service.callTool("submit_review_drafts", {
      projectRoot: root,
      phase: "input_review",
    });
    expect(result.ok).toBe(true);
    const serialized = JSON.stringify(result);
    expect(serialized).toContain("男人肤色与前页不一致");
    expect(serialized).not.toContain("defect_codes");
    expect(serialized).not.toContain("required_state");
    expect(serialized).not.toContain("targets");
  });

  it("delegates normalized commits to the validator boundary", async () => {
    const root = makeProject();
    const commit = vi.fn(async () => ({
      status: "committed" as const,
      artifacts: ["evidence/human_visual_selection.json"],
    }));
    const service = createWorkbenchToolService({ commitNormalized: commit });
    const request = {
      projectRoot: root,
      runRelativePath: "production_runs/run-001",
      selectionDocument: { status: "confirmed" },
      annotationDocument: null,
      revisionDocument: null,
    };

    const result = await service.callTool(
      "commit_normalized_annotations",
      request,
    );
    expect(commit).toHaveBeenCalledOnce();
    expect(commit).toHaveBeenCalledWith(request);
    expect(result).toMatchObject({ ok: true, data: { status: "committed" } });
  });

  it("persists output decisions and returns evidence-backed status", async () => {
    const root = makeProject();
    writeFileSync(join(root, "输出", "0001.jpg"), "candidate-one");
    const readStatus = vi.fn(async () => ({ blockers: [], pendingHumanReview: 1 }));
    const service = createWorkbenchToolService({ readStatus });
    await service.callTool("open_comic_review_workbench", {
      projectRoot: root,
      mode: "automatic",
    });
    await service.callTool("submit_review_drafts", {
      projectRoot: root,
      phase: "input_review",
    });
    const decision = await service.callTool("record_output_page_decision", {
      projectRoot: root,
      page: "0001.jpg",
      decision: { state: "passed" },
    });
    expect(decision).toMatchObject({
      ok: true,
      data: { status: "saved", reviewState: "locked" },
    });
    const reopened = await service.callTool("open_comic_review_workbench", {
      projectRoot: root,
      mode: "automatic",
    });
    expect(reopened).toMatchObject({
      ok: true,
      data: { phase: "output_review", pages: [{ reviewState: "locked" }] },
    });

    writeFileSync(join(root, "输出", "0001.jpg"), "candidate-two");
    const changed = await service.callTool("open_comic_review_workbench", {
      projectRoot: root,
      mode: "automatic",
    });
    expect(changed).toMatchObject({
      ok: true,
      data: { pages: [{ reviewState: "unreviewed" }] },
    });
    const status = await service.callTool("get_comic_review_status", {
      projectRoot: root,
    });
    expect(status).toEqual({
      ok: true,
      data: { blockers: [], pendingHumanReview: 1 },
    });
  });
});
