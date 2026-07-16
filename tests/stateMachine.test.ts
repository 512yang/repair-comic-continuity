import { describe, expect, it } from "vitest";

import { deriveSubmitAction } from "../src/shared/stateMachine";

describe("deriveSubmitAction", () => {
  it("uses one button for first submission and revisions", () => {
    expect(
      deriveSubmitAction({
        phase: "input_review",
        dirty: true,
        running: false,
        blockers: [],
      }),
    ).toBe("initial_submit");
    expect(
      deriveSubmitAction({
        phase: "output_review",
        dirty: true,
        running: false,
        blockers: [],
      }),
    ).toBe("revision_submit");
  });

  it("blocks duplicate, running, and incomplete submissions", () => {
    expect(
      deriveSubmitAction({
        phase: "input_review",
        dirty: false,
        running: false,
        blockers: [],
      }),
    ).toBe("disabled_no_changes");
    expect(
      deriveSubmitAction({
        phase: "processing",
        dirty: true,
        running: true,
        blockers: [],
      }),
    ).toBe("disabled_running");
    expect(
      deriveSubmitAction({
        phase: "output_review",
        dirty: true,
        running: false,
        blockers: ["2 pages unreviewed"],
      }),
    ).toBe("disabled_blocked");
  });

  it("rejects phases that cannot submit", () => {
    expect(() =>
      deriveSubmitAction({
        phase: "project_setup",
        dirty: true,
        running: false,
        blockers: [],
      }),
    ).toThrow(/cannot submit/i);
  });
});
