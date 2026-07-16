// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnnotationCanvas } from "../src/widget/components/AnnotationCanvas";

afterEach(cleanup);

describe("AnnotationCanvas", () => {
  it("stores a rectangle in normalized image coordinates", () => {
    const onShapesChange = vi.fn();
    render(
      <AnnotationCanvas
        page="0001.jpg"
        sourceUrl="comic-page://session/input/0"
        shapes={[]}
        onShapesChange={onShapesChange}
      />,
    );
    const overlay = screen.getByTestId("annotation-overlay");
    vi.spyOn(overlay, "getBoundingClientRect").mockReturnValue({
      left: 10,
      top: 20,
      width: 200,
      height: 400,
      right: 210,
      bottom: 420,
      x: 10,
      y: 20,
      toJSON: () => ({}),
    });

    fireEvent.pointerDown(overlay, { clientX: 30, clientY: 60, pointerId: 1 });
    fireEvent.pointerMove(overlay, { clientX: 110, clientY: 220, pointerId: 1 });
    fireEvent.pointerUp(overlay, { clientX: 110, clientY: 220, pointerId: 1 });

    expect(onShapesChange).toHaveBeenLastCalledWith([
      { shape_kind: "rectangle", bbox_norm: [0.1, 0.1, 0.5, 0.5] },
    ]);
  });

  it("adds full-page evidence and can undo it", async () => {
    const onShapesChange = vi.fn();
    const { rerender } = render(
      <AnnotationCanvas
        page="0001.jpg"
        sourceUrl="comic-page://session/input/0"
        shapes={[]}
        onShapesChange={onShapesChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "整页" }));
    const next = [{ shape_kind: "full_page" as const, bbox_norm: [0, 0, 1, 1] as [number, number, number, number] }];
    expect(onShapesChange).toHaveBeenLastCalledWith(next);

    rerender(
      <AnnotationCanvas
        page="0001.jpg"
        sourceUrl="comic-page://session/input/0"
        shapes={next}
        onShapesChange={onShapesChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(onShapesChange).toHaveBeenLastCalledWith([]);
  });
});
