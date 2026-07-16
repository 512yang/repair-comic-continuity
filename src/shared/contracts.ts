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

export type AnnotationShapeKind =
  | "rectangle"
  | "polygon"
  | "freehand"
  | "full_page";

export type Point = readonly [number, number];

export interface RawAnnotationShape {
  kind: AnnotationShapeKind;
  points: readonly Point[];
}

export interface ImageDimensions {
  width: number;
  height: number;
}

export interface NormalizedAnnotationShape {
  shape_kind: AnnotationShapeKind;
  bbox_norm: [number, number, number, number];
  polygon_norm?: Point[];
  freehand_norm?: Point[];
}
