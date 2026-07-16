import type { PageReviewState, ReviewMode, RunPhase } from "../shared/contracts";
import type { WorkbenchInitialState } from "./App";

interface OpenPageDescriptor {
  path: string;
  sourceUri: string;
  outputUri?: string;
  reviewState?: PageReviewState;
}

export async function buildInitialStateFromToolResult(
  result: unknown,
  readImage: (uri: string) => Promise<string>,
): Promise<WorkbenchInitialState | null> {
  const root = record(result);
  const structured = record(root?.structuredContent);
  const envelope = structured ?? root;
  if (!envelope || envelope.ok !== true) return null;
  const data = record(envelope.data);
  if (!data || !Array.isArray(data.pages)) return null;
  const projectRoot = nonemptyString(data.projectRoot);
  const phase = runPhase(data.phase);
  const mode = reviewMode(data.mode);
  if (!projectRoot || !phase || !mode) return null;

  const descriptors = data.pages.map(pageDescriptor);
  if (descriptors.some((page) => page === null)) return null;
  const pages = await Promise.all(
    (descriptors as OpenPageDescriptor[]).map(async (page) => ({
      path: page.path,
      sourceUrl: await readImage(page.sourceUri),
      outputUrl:
        phase === "output_review" && page.outputUri
          ? await readImage(page.outputUri)
          : undefined,
      ...(page.reviewState ? { reviewState: page.reviewState } : {}),
    })),
  );
  return { projectRoot, phase, mode, pages };
}

function pageDescriptor(value: unknown): OpenPageDescriptor | null {
  const page = record(value);
  if (!page) return null;
  const path = nonemptyString(page.path);
  const sourceUri = nonemptyString(page.sourceUri);
  const outputUri = nonemptyString(page.outputUri) ?? undefined;
  const reviewState = pageReviewState(page.reviewState) ?? undefined;
  return path && sourceUri ? { path, sourceUri, outputUri, reviewState } : null;
}

function pageReviewState(value: unknown): PageReviewState | null {
  return [
    "unreviewed",
    "correct",
    "annotated",
    "processing",
    "passed",
    "needs_revision",
    "locked",
  ].includes(String(value))
    ? (value as PageReviewState)
    : null;
}

function runPhase(value: unknown): RunPhase | null {
  return [
    "project_setup",
    "input_review",
    "processing",
    "output_review",
    "blocked",
    "complete",
  ].includes(String(value))
    ? (value as RunPhase)
    : null;
}

function reviewMode(value: unknown): ReviewMode | null {
  return value === "human_visual_auto_text" || value === "automatic" ? value : null;
}

function nonemptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}
