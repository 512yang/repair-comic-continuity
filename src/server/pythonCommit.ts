import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

import { writeAtomicJson } from "./atomicJson";
import { resolveInside, sha256File } from "./projectInventory";
import type { NormalizedCommitRequest } from "./tools";

interface PythonBridgeResult {
  status: "validated" | "blocked";
  error?: string;
  documents?: {
    selection?: unknown;
    annotations?: unknown;
    revision?: { attempt: number } & Record<string, unknown>;
  };
}

export function commitNormalizedWithPython(
  request: NormalizedCommitRequest,
): { status: "committed"; artifacts: string[]; receiptSha256: string } {
  const projectRoot = resolve(request.projectRoot);
  const runRoot = resolveInside(projectRoot, request.runRelativePath);
  if (!existsSync(runRoot)) {
    throw new Error(`Isolated run root does not exist: ${runRoot}`);
  }
  const staging = join(
    projectRoot,
    ".comic-review-workbench",
    "commit-staging",
    randomUUID(),
  );
  mkdirSync(staging, { recursive: true });
  const requestPath = join(staging, "request.json");
  writeAtomicJson(requestPath, {
    project_root: projectRoot,
    run_root: runRoot,
    selection_document: request.selectionDocument,
    annotation_document: request.annotationDocument,
    revision_document: request.revisionDocument,
  });

  const skillRoot = resolve("skills", "repair-comic-continuity");
  const bridge = join(skillRoot, "scripts", "workbench_commit_bridge.py");
  const executed = spawnSync(
    process.env.PYTHON ?? "python",
    [bridge, "--request", requestPath],
    {
      cwd: skillRoot,
      encoding: "utf8",
      env: { ...process.env, PYTHONUTF8: "1" },
      shell: false,
      windowsHide: true,
    },
  );
  if (executed.error) {
    throw new Error(`Python validator failed to start: ${executed.error.message}`);
  }
  const line = executed.stdout.trim().split(/\r?\n/u).at(-1) ?? "";
  let result: PythonBridgeResult;
  try {
    result = JSON.parse(line) as PythonBridgeResult;
  } catch {
    throw new Error(`Python validator returned invalid JSON: ${executed.stderr.trim()}`);
  }
  if (executed.status !== 0 || result.status !== "validated" || !result.documents) {
    throw new Error(
      result.error ?? (executed.stderr.trim() || "Python validation blocked"),
    );
  }

  const evidenceRoot = join(runRoot, "evidence");
  const artifacts: string[] = [];
  const commitDocument = (relativePath: string, value: unknown): void => {
    const path = resolveInside(runRoot, `evidence/${relativePath}`);
    if (relativePath.startsWith("human_revision_feedback/") && existsSync(path)) {
      throw new Error(`Revision feedback attempt already exists: ${relativePath}`);
    }
    mkdirSync(dirname(path), { recursive: true });
    writeAtomicJson(path, value);
    artifacts.push(`evidence/${relativePath}`);
  };
  if (result.documents.selection !== undefined) {
    commitDocument("human_visual_selection.json", result.documents.selection);
  }
  if (result.documents.annotations !== undefined) {
    commitDocument("human_issue_annotations.json", result.documents.annotations);
  }
  if (result.documents.revision !== undefined) {
    commitDocument(
      `human_revision_feedback/attempt-${result.documents.revision.attempt}.json`,
      result.documents.revision,
    );
  }
  const boundArtifacts = artifacts.map((relativePath) => ({
    path: relativePath,
    sha256: sha256File(join(runRoot, ...relativePath.split("/"))),
  }));
  const receipt = {
    schema_version: "comic-review-workbench-commit-receipt-v1",
    artifacts: boundArtifacts,
    committed_at: new Date().toISOString(),
  };
  const receiptSha256 = createHash("sha256")
    .update(JSON.stringify(receipt))
    .digest("hex");
  writeAtomicJson(join(evidenceRoot, "workbench_commit_receipt.json"), {
    ...receipt,
    receipt_sha256: receiptSha256,
  });
  artifacts.push("evidence/workbench_commit_receipt.json");
  return { status: "committed", artifacts, receiptSha256 };
}
