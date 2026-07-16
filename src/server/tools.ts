import type { DraftPhase } from "./draftStore";
import { listDrafts, saveDraft } from "./draftStore";
import {
  createProjectSession,
  openProjectSession,
} from "./projectSession";
import type { ReviewMode } from "../shared/contracts";
import { inspectOutputReview, recordOutputDecision } from "./outputReview";

export const TOOL_NAMES = [
  "open_comic_review_workbench",
  "save_review_draft",
  "submit_review_drafts",
  "commit_normalized_annotations",
  "get_comic_review_status",
  "record_output_page_decision",
] as const;

export type WorkbenchToolName = (typeof TOOL_NAMES)[number];

export interface NormalizedCommitRequest {
  projectRoot: string;
  runRelativePath: string;
  selectionDocument: unknown;
  annotationDocument: unknown | null;
  revisionDocument: unknown | null;
}

export interface WorkbenchToolDependencies {
  commitNormalized?: (
    request: NormalizedCommitRequest,
  ) => Promise<unknown> | unknown;
  readStatus?: (input: { projectRoot: string }) => Promise<unknown> | unknown;
  onSessionOpened?: (session: {
    sessionId: string;
    projectRoot: string;
    inputRoot: string;
    pages: Array<{ path: string; sha256: string; bytes: number }>;
  }) => void;
}

export type ToolCallResult =
  | { ok: true; data: unknown }
  | { ok: false; blocker: { code: string; message: string } };

export function createWorkbenchToolService(
  dependencies: WorkbenchToolDependencies = {},
) {
  const handlers: Record<
    WorkbenchToolName,
    (input: Record<string, unknown>) => Promise<unknown>
  > = {
    open_comic_review_workbench: async (input) => {
      const projectRoot = requiredString(input.projectRoot, "projectRoot");
      const mode = reviewMode(input.mode);
      const session = createProjectSession(projectRoot, mode);
      const output = inspectOutputReview(session.projectRoot, session.inventory.pages);
      const phase = session.modeLockedAt && output.ready ? "output_review" : session.phase;
      dependencies.onSessionOpened?.({
        sessionId: session.sessionId,
        projectRoot: session.projectRoot,
        inputRoot: session.inventory.input_root,
        pages: session.inventory.pages,
      });
      return {
        sessionId: session.sessionId,
        projectRoot: session.projectRoot,
        mode: session.mode,
        modeLockedAt: session.modeLockedAt,
        phase,
        inventory: session.inventory,
        outputReview: {
          ready: output.ready,
          missingPages: output.missingPages,
          unexpectedPages: output.unexpectedPages,
        },
        pages: session.inventory.pages.map((page, index) => ({
          path: page.path,
          sourceUri: `comic-page://${session.sessionId}/input/${index}`,
          outputUri: `comic-page://${session.sessionId}/output/${index}`,
          reviewState: output.pages[index]?.reviewState ?? "unreviewed",
        })),
      };
    },
    save_review_draft: async (input) => {
      const projectRoot = requiredString(input.projectRoot, "projectRoot");
      const phase = draftPhase(input.phase);
      const page = requiredString(input.page, "page");
      if (!("draft" in input)) {
        throw new Error("draft is required");
      }
      saveDraft(projectRoot, phase, page, input.draft);
      return { status: "saved", page, phase };
    },
    submit_review_drafts: async (input) => {
      const projectRoot = requiredString(input.projectRoot, "projectRoot");
      const phase = draftPhase(input.phase);
      const session = openProjectSession(projectRoot);
      const drafts = listDrafts<Record<string, unknown>>(projectRoot, phase);
      if (phase === "input_review" && session.mode === "human_visual_auto_text") {
        const reviewed = new Set(
          drafts
            .filter((draft) =>
              ["correct", "annotated"].includes(String(draft.value.state)),
            )
            .map((draft) => draft.page),
        );
        const missing = session.inventory.pages
          .map((page) => page.path)
          .filter((page) => !reviewed.has(page));
        if (missing.length > 0) {
          throw new Error(`${missing.length} input pages are still unreviewed`);
        }
      }
      if (phase === "output_review") {
        const output = inspectOutputReview(projectRoot, session.inventory.pages);
        if (!output.ready) {
          throw new Error("Output names/count do not exactly match the sealed input inventory.");
        }
        const missing = output.pages.filter(
          (page) => page.reviewState !== "locked" && page.reviewState !== "needs_revision",
        );
        if (missing.length > 0) {
          throw new Error(`${missing.length} output pages are still unreviewed`);
        }
      }
      session.lockMode(session.mode);
      return {
        schema_version: "comic-review-workbench-raw-submit-v1",
        status: "raw_user_drafts",
        mode: session.mode,
        phase,
        drafts,
      };
    },
    commit_normalized_annotations: async (input) => {
      if (!dependencies.commitNormalized) {
        throw new Error("normalized annotation validator is unavailable");
      }
      const request: NormalizedCommitRequest = {
        projectRoot: requiredString(input.projectRoot, "projectRoot"),
        runRelativePath: requiredString(
          input.runRelativePath,
          "runRelativePath",
        ),
        selectionDocument: input.selectionDocument,
        annotationDocument: input.annotationDocument ?? null,
        revisionDocument: input.revisionDocument ?? null,
      };
      return dependencies.commitNormalized(request);
    },
    get_comic_review_status: async (input) => {
      const projectRoot = requiredString(input.projectRoot, "projectRoot");
      if (!dependencies.readStatus) {
        return {
          blockers: ["progress_reader_not_connected"],
          pendingHumanReview: null,
        };
      }
      return dependencies.readStatus({ projectRoot });
    },
    record_output_page_decision: async (input) => {
      const projectRoot = requiredString(input.projectRoot, "projectRoot");
      const page = requiredString(input.page, "page");
      if (!("decision" in input)) {
        throw new Error("decision is required");
      }
      const session = openProjectSession(projectRoot);
      if (!session.modeLockedAt) {
        throw new Error("Output review cannot begin before the processing mode is submitted.");
      }
      const recorded = recordOutputDecision(
        projectRoot,
        session.inventory.pages,
        page,
        input.decision,
      );
      return { status: "saved", page, phase: "output_review", ...recorded };
    },
  };

  return {
    listToolNames: (): readonly WorkbenchToolName[] => TOOL_NAMES,
    async callTool(name: WorkbenchToolName, input: unknown): Promise<ToolCallResult> {
      try {
        if (!TOOL_NAMES.includes(name) || !isRecord(input)) {
          throw new Error("Unknown tool or invalid tool input.");
        }
        return { ok: true, data: await handlers[name](input) };
      } catch (error) {
        return {
          ok: false,
          blocker: {
            code: "workbench_tool_blocked",
            message: error instanceof Error ? error.message : String(error),
          },
        };
      }
    },
  };
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${field} must be a nonempty string`);
  }
  return value.trim();
}

function reviewMode(value: unknown): ReviewMode {
  if (value !== "human_visual_auto_text" && value !== "automatic") {
    throw new Error("mode must be human_visual_auto_text or automatic");
  }
  return value;
}

function draftPhase(value: unknown): DraftPhase {
  if (value !== "input_review" && value !== "output_review") {
    throw new Error("phase must be input_review or output_review");
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
