import { describe, expect, it, vi } from "vitest";

import { buildInitialStateFromToolResult } from "../src/widget/bootstrap";

describe("widget bootstrap", () => {
  it("turns the open-tool result into renderable pages without exposing disk paths", async () => {
    const readImage = vi.fn(async (uri: string) => `data:image/png;base64,${uri}`);
    const state = await buildInitialStateFromToolResult(
      {
        structuredContent: {
          ok: true,
          data: {
            projectRoot: "D:/demo",
            phase: "project_setup",
            mode: "human_visual_auto_text",
            pages: [
              {
                path: "0001.png",
                sourceUri: "comic-page://opaque/input/0",
                outputUri: "comic-page://opaque/output/0",
              },
            ],
          },
        },
      },
      readImage,
    );

    expect(readImage).toHaveBeenCalledWith("comic-page://opaque/input/0");
    expect(state).toEqual({
      projectRoot: "D:/demo",
      phase: "project_setup",
      mode: "human_visual_auto_text",
      pages: [
        {
          path: "0001.png",
          sourceUrl: "data:image/png;base64,comic-page://opaque/input/0",
          outputUrl: undefined,
        },
      ],
    });
  });

  it("ignores unrelated tool results", async () => {
    await expect(
      buildInitialStateFromToolResult(
        { structuredContent: { ok: true, data: { status: "saved" } } },
        vi.fn(),
      ),
    ).resolves.toBeNull();
  });
});
