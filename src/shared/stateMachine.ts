import type { SubmitAction, SubmitContext } from "./contracts";

export function deriveSubmitAction(context: SubmitContext): SubmitAction {
  if (context.blockers.length > 0) {
    return "disabled_blocked";
  }
  if (context.running) {
    return "disabled_running";
  }
  if (!context.dirty) {
    return "disabled_no_changes";
  }
  if (context.phase === "input_review") {
    return "initial_submit";
  }
  if (context.phase === "output_review") {
    return "revision_submit";
  }
  throw new Error(`Phase ${context.phase} cannot submit review changes.`);
}
