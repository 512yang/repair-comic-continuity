export type ReviewMode = "human_visual_auto_text" | "automatic";

export type RunPhase =
  | "project_setup"
  | "input_review"
  | "processing"
  | "output_review"
  | "blocked"
  | "complete";

export type PageReviewState =
  | "unreviewed"
  | "correct"
  | "annotated"
  | "processing"
  | "passed"
  | "needs_revision"
  | "locked";

export type SubmitAction =
  | "initial_submit"
  | "revision_submit"
  | "disabled_no_changes"
  | "disabled_running"
  | "disabled_blocked";

export interface SubmitContext {
  phase: RunPhase;
  dirty: boolean;
  running: boolean;
  blockers: string[];
}
