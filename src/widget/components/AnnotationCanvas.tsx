import { useState } from "react";

import type { NormalizedAnnotationShape, Point } from "../../shared/contracts";

type DrawTool = "rectangle" | "freehand";

interface AnnotationCanvasProps {
  page: string;
  sourceUrl: string;
  shapes: NormalizedAnnotationShape[];
  onShapesChange: (shapes: NormalizedAnnotationShape[]) => void;
}

interface ActiveStroke {
  tool: DrawTool;
  points: Point[];
}

export function AnnotationCanvas({
  page,
  sourceUrl,
  shapes,
  onShapesChange,
}: AnnotationCanvasProps) {
  const [tool, setTool] = useState<DrawTool>("rectangle");
  const [active, setActive] = useState<ActiveStroke | null>(null);

  const pointFromEvent = (event: React.PointerEvent<SVGSVGElement>): Point => {
    const box = event.currentTarget.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return [0, 0];
    return [
      clamp01((event.clientX - box.left) / box.width),
      clamp01((event.clientY - box.top) / box.height),
    ];
  };

  const finish = (stroke: ActiveStroke): void => {
    const [first] = stroke.points;
    const last = stroke.points.at(-1);
    if (!first || !last || (first[0] === last[0] && first[1] === last[1])) return;
    const bbox = bboxFor(stroke.points);
    const shape: NormalizedAnnotationShape =
      stroke.tool === "freehand"
        ? { shape_kind: "freehand", bbox_norm: bbox, freehand_norm: stroke.points }
        : { shape_kind: "rectangle", bbox_norm: bbox };
    onShapesChange([...shapes, shape]);
  };

  return (
    <div className="annotation-canvas" aria-label={`圈画 ${page}`}>
      <div className="draw-toolbar" aria-label="圈画工具">
        <button
          type="button"
          className={tool === "rectangle" ? "active" : ""}
          onClick={() => setTool("rectangle")}
        >
          矩形
        </button>
        <button
          type="button"
          className={tool === "freehand" ? "active" : ""}
          onClick={() => setTool("freehand")}
        >
          自由笔
        </button>
        <button
          type="button"
          onClick={() =>
            onShapesChange([
              ...shapes,
              { shape_kind: "full_page", bbox_norm: [0, 0, 1, 1] },
            ])
          }
        >
          整页
        </button>
        <button type="button" onClick={() => onShapesChange(shapes.slice(0, -1))}>
          撤销
        </button>
      </div>
      <div className="image-stage">
        <img src={sourceUrl} alt={page} draggable={false} />
        <svg
          data-testid="annotation-overlay"
          className="annotation-overlay"
          viewBox="0 0 1 1"
          preserveAspectRatio="none"
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture?.(event.pointerId);
            setActive({ tool, points: [pointFromEvent(event)] });
          }}
          onPointerMove={(event) => {
            if (!active) return;
            const point = pointFromEvent(event);
            setActive({
              ...active,
              points:
                active.tool === "rectangle"
                  ? [active.points[0], point]
                  : [...active.points, point],
            });
          }}
          onPointerUp={(event) => {
            if (!active) return;
            const point = pointFromEvent(event);
            const stroke = {
              ...active,
              points:
                active.tool === "rectangle"
                  ? [active.points[0], point]
                  : [...active.points, point],
            };
            finish(stroke);
            setActive(null);
          }}
        >
          {shapes.map((shape, index) => (
            <ShapeMark key={`${page}-${index}`} shape={shape} />
          ))}
          {active ? <ShapeMark shape={activeShape(active)} preview /> : null}
        </svg>
      </div>
    </div>
  );
}

function ShapeMark({
  shape,
  preview = false,
}: {
  shape: NormalizedAnnotationShape;
  preview?: boolean;
}) {
  const [x1, y1, x2, y2] = shape.bbox_norm;
  const className = preview ? "annotation-mark preview" : "annotation-mark";
  if (shape.shape_kind === "freehand" && shape.freehand_norm) {
    return (
      <polyline
        className={className}
        points={shape.freehand_norm.map(([x, y]) => `${x},${y}`).join(" ")}
        fill="none"
      />
    );
  }
  return (
    <rect
      className={className}
      x={x1}
      y={y1}
      width={x2 - x1}
      height={y2 - y1}
      fill="none"
    />
  );
}

function activeShape(stroke: ActiveStroke): NormalizedAnnotationShape {
  const bbox = bboxFor(stroke.points);
  return stroke.tool === "freehand"
    ? { shape_kind: "freehand", bbox_norm: bbox, freehand_norm: stroke.points }
    : { shape_kind: "rectangle", bbox_norm: bbox };
}

function bboxFor(points: readonly Point[]): [number, number, number, number] {
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}
