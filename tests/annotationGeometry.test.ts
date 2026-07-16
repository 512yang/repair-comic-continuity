import { describe, expect, it } from "vitest";

import { normalizeShape } from "../src/shared/annotationGeometry";

describe("normalizeShape", () => {
  it("preserves polygon geometry and derives a compatible bounding box", () => {
    const polygon = normalizeShape(
      {
        kind: "polygon",
        points: [
          [100, 50],
          [300, 80],
          [250, 200],
        ],
      },
      { width: 1000, height: 500 },
    );
    expect(polygon.bbox_norm).toEqual([0.1, 0.1, 0.3, 0.4]);
    expect(polygon.polygon_norm).toEqual([
      [0.1, 0.1],
      [0.3, 0.16],
      [0.25, 0.4],
    ]);
  });

  it("supports rectangle, freehand, and full-page regions", () => {
    expect(
      normalizeShape(
        { kind: "rectangle", points: [[10, 20], [90, 70]] },
        { width: 100, height: 100 },
      ).bbox_norm,
    ).toEqual([0.1, 0.2, 0.9, 0.7]);
    expect(
      normalizeShape(
        { kind: "freehand", points: [[10, 20], [40, 60], [90, 70]] },
        { width: 100, height: 100 },
      ).freehand_norm,
    ).toHaveLength(3);
    expect(
      normalizeShape(
        { kind: "full_page", points: [] },
        { width: 100, height: 100 },
      ).bbox_norm,
    ).toEqual([0, 0, 1, 1]);
  });

  it("rejects invalid dimensions, out-of-bounds points, and degenerate shapes", () => {
    expect(() =>
      normalizeShape(
        { kind: "full_page", points: [] },
        { width: 0, height: 500 },
      ),
    ).toThrow(/positive dimensions/i);
    expect(() =>
      normalizeShape(
        { kind: "rectangle", points: [[10, 20], [101, 70]] },
        { width: 100, height: 100 },
      ),
    ).toThrow(/image bounds/i);
    expect(() =>
      normalizeShape(
        { kind: "polygon", points: [[10, 20], [20, 20], [30, 20]] },
        { width: 100, height: 100 },
      ),
    ).toThrow(/positive area/i);
  });
});
