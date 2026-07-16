import { existsSync, readdirSync, statSync } from "node:fs";
import { relative, resolve, sep } from "node:path";

import type { PageReviewState } from "../shared/contracts";
import { listDrafts, saveDraft } from "./draftStore";
import type { InventoryFile } from "./projectInventory";
import { resolveInside, sha256File } from "./projectInventory";

const IMAGE_EXTENSIONS = new Set([
  ".bmp",
  ".gif",
  ".jpeg",
  ".jpg",
  ".png",
  ".tif",
  ".tiff",
  ".webp",
]);

interface StoredOutputDecision {
  state: "passed" | "needs_revision";
  note: string;
  shapes: unknown[];
  candidateSha256: string;
}

export interface OutputReviewPage {
  path: string;
  available: boolean;
  candidateSha256: string | null;
  reviewState: PageReviewState;
}

export interface OutputReviewSnapshot {
  ready: boolean;
  pages: OutputReviewPage[];
  unexpectedPages: string[];
  missingPages: string[];
}

export function inspectOutputReview(
  projectRoot: string,
  expectedPages: readonly InventoryFile[],
): OutputReviewSnapshot {
  const outputRoot = resolve(projectRoot, "输出");
  const expected = new Set(expectedPages.map((page) => page.path));
  const actual = existsSync(outputRoot) ? listImagePaths(outputRoot) : [];
  const actualSet = new Set(actual);
  const missingPages = expectedPages
    .map((page) => page.path)
    .filter((path) => !actualSet.has(path));
  const unexpectedPages = actual.filter((path) => !expected.has(path));
  const drafts = new Map(
    listDrafts<StoredOutputDecision>(projectRoot, "output_review").map((draft) => [
      draft.page,
      draft.value,
    ]),
  );
  const pages = expectedPages.map((page): OutputReviewPage => {
    const available = actualSet.has(page.path);
    const candidateSha256 = available
      ? sha256File(resolveInside(outputRoot, page.path))
      : null;
    const draft = drafts.get(page.path);
    let reviewState: PageReviewState = "unreviewed";
    if (candidateSha256 && draft?.candidateSha256 === candidateSha256) {
      reviewState = draft.state === "passed" ? "locked" : "needs_revision";
    }
    return { path: page.path, available, candidateSha256, reviewState };
  });
  return {
    ready: missingPages.length === 0 && unexpectedPages.length === 0,
    pages,
    unexpectedPages,
    missingPages,
  };
}

export function recordOutputDecision(
  projectRoot: string,
  expectedPages: readonly InventoryFile[],
  page: string,
  decision: unknown,
): { reviewState: "locked" | "needs_revision"; candidateSha256: string } {
  const input = asRecord(decision);
  const state = input?.state;
  if (state !== "passed" && state !== "needs_revision") {
    throw new Error("Output decision state must be passed or needs_revision.");
  }
  const snapshot = inspectOutputReview(projectRoot, expectedPages);
  if (!snapshot.ready) {
    throw new Error("Output names/count do not exactly match the sealed input inventory.");
  }
  const candidate = snapshot.pages.find((item) => item.path === page);
  if (!candidate?.candidateSha256) {
    throw new Error(`Output page is unavailable or outside the sealed inventory: ${page}`);
  }
  if (candidate.reviewState === "locked") {
    if (state === "passed") {
      return { reviewState: "locked", candidateSha256: candidate.candidateSha256 };
    }
    throw new Error("A passed candidate is locked until its file hash changes.");
  }
  const note = typeof input?.note === "string" ? input.note.trim() : "";
  const shapes = Array.isArray(input?.shapes) ? input.shapes : [];
  if (state === "needs_revision" && !note && shapes.length === 0) {
    throw new Error("A revision decision requires a note or annotation shape.");
  }
  saveDraft(projectRoot, "output_review", page, {
    state,
    note,
    shapes,
    candidateSha256: candidate.candidateSha256,
  } satisfies StoredOutputDecision);
  return {
    reviewState: state === "passed" ? "locked" : "needs_revision",
    candidateSha256: candidate.candidateSha256,
  };
}

function listImagePaths(root: string): string[] {
  const found: string[] = [];
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const absolute = resolve(directory, entry.name);
      if (entry.isDirectory()) visit(absolute);
      else if (entry.isFile() && IMAGE_EXTENSIONS.has(extension(entry.name))) {
        found.push(relative(root, absolute).split(sep).join("/"));
      }
    }
  };
  if (existsSync(root) && statSync(root).isDirectory()) visit(root);
  return found.sort(new Intl.Collator("zh-CN", { numeric: true }).compare);
}

function extension(path: string): string {
  const index = path.lastIndexOf(".");
  return index < 0 ? "" : path.slice(index).toLocaleLowerCase();
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}
