import type { PageReviewState } from "../../shared/contracts";

export interface WorkbenchPage {
  path: string;
  sourceUrl: string;
  outputUrl?: string;
  reviewState?: PageReviewState;
}

interface PageRailProps {
  pages: WorkbenchPage[];
  currentIndex: number;
  states: Record<string, PageReviewState>;
  onSelect: (index: number) => void;
}

const labels: Partial<Record<PageReviewState, string>> = {
  correct: "正确",
  annotated: "有批注",
  passed: "通过",
  needs_revision: "需二修",
  locked: "已锁定",
};

export function PageRail({ pages, currentIndex, states, onSelect }: PageRailProps) {
  return (
    <nav className="page-rail" aria-label="漫画页面">
      <div className="rail-heading">
        <strong>页面</strong>
        <span>{pages.length}</span>
      </div>
      <ol>
        {pages.map((page, index) => {
          const state = states[page.path] ?? "unreviewed";
          return (
            <li key={page.path}>
              <button
                type="button"
                className={index === currentIndex ? "page-item active" : "page-item"}
                onClick={() => onSelect(index)}
              >
                <span className="page-number">{index + 1}</span>
                <span className="page-name">{page.path}</span>
                <span className={`page-state state-${state}`}>
                  {labels[state] ?? "未审"}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
