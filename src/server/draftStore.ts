import { join, resolve } from "node:path";

import { readAtomicJson, writeAtomicJson } from "./atomicJson";
import { assertSafeRelativePath } from "./projectInventory";

export type DraftPhase = "input_review" | "output_review";

interface StoredDraft<T> {
  schema_version: "comic-review-workbench-draft-v1";
  page: string;
  phase: DraftPhase;
  value: T;
  updated_at: string;
}

export function saveDraft<T>(
  projectRoot: string,
  phase: DraftPhase,
  page: string,
  value: T,
): void {
  const safePage = assertSafeRelativePath(page);
  writeAtomicJson(draftPath(projectRoot, phase, safePage), {
    schema_version: "comic-review-workbench-draft-v1",
    page: safePage,
    phase,
    value,
    updated_at: new Date().toISOString(),
  } satisfies StoredDraft<T>);
}

export function loadDraft<T>(
  projectRoot: string,
  phase: DraftPhase,
  page: string,
): T {
  const safePage = assertSafeRelativePath(page);
  const stored = readAtomicJson<StoredDraft<T>>(
    draftPath(projectRoot, phase, safePage),
  );
  if (stored.page !== safePage || stored.phase !== phase) {
    throw new Error("Stored draft identity does not match the requested page and phase.");
  }
  return stored.value;
}

function draftPath(
  projectRoot: string,
  phase: DraftPhase,
  page: string,
): string {
  return join(
    resolve(projectRoot),
    ".comic-review-workbench",
    "drafts",
    phase,
    `${encodeURIComponent(page)}.json`,
  );
}
