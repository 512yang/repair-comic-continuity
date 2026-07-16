import type {
  ImageDimensions,
  NormalizedAnnotationShape,
  Point,
  RawAnnotationShape,
} from "./contracts";

export function normalizeShape(
  shape: RawAnnotationShape,
  dimensions: ImageDimensions,
): NormalizedAnnotationShape {
  if (
    !Number.isFinite(dimensions.width) ||
    !Number.isFinite(dimensions.height) ||
    dimensions.width <= 0 ||
    dimensions.height <= 0
  ) {
    throw new Error("Image must have positive dimensions.");
  }
  if (shape.kind === "full_page") {
    if (shape.points.length !== 0) {
      throw new Error("A full-page annotation cannot contain points.");
    }
    return { shape_kind: "full_page", bbox_norm: [0, 0, 1, 1] };
  }

  const requiredPoints = shape.kind === "rectangle" ? 2 : shape.kind === "polygon" ? 3 : 2;
  if (
    (shape.kind === "rectangle" && shape.points.length !== requiredPoints) ||
    (shape.kind !== "rectangle" && shape.points.length < requiredPoints)
  ) {
    throw new Error(`${shape.kind} requires ${requiredPoints} valid points.`);
  }

  const normalized = shape.points.map(([x, y]): Point => {
    if (
      !Number.isFinite(x) ||
      !Number.isFinite(y) ||
      x < 0 ||
      y < 0 ||
      x > dimensions.width ||
      y > dimensions.height
    ) {
      throw new Error("Annotation points must remain inside image bounds.");
    }
    return [x / dimensions.width, y / dimensions.height];
  });
  const xs = normalized.map(([x]) => x);
  const ys = normalized.map(([, y]) => y);
  const bbox: [number, number, number, number] = [
    Math.min(...xs),
    Math.min(...ys),
    Math.max(...xs),
    Math.max(...ys),
  ];
  if (bbox[0] >= bbox[2] || bbox[1] >= bbox[3]) {
    throw new Error("Annotation shapes must have positive area.");
  }

  if (shape.kind === "polygon") {
    return { shape_kind: shape.kind, bbox_norm: bbox, polygon_norm: normalized };
  }
  if (shape.kind === "freehand") {
    return { shape_kind: shape.kind, bbox_norm: bbox, freehand_norm: normalized };
  }
  return { shape_kind: shape.kind, bbox_norm: bbox };
}
