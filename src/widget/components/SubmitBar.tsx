import type { SubmitAction } from "../../shared/contracts";

interface SubmitBarProps {
  submitAction: SubmitAction;
  reviewed: number;
  total: number;
  showCorrect: boolean;
  canMarkIssue: boolean;
  onCorrect: () => void;
  onMarkIssue: () => void;
  onSubmit: () => void;
}

export function SubmitBar({
  submitAction,
  reviewed,
  total,
  showCorrect,
  canMarkIssue,
  onCorrect,
  onMarkIssue,
  onSubmit,
}: SubmitBarProps) {
  const disabled = submitAction.startsWith("disabled_");
  return (
    <footer className="submit-bar">
      <span className="review-count">{reviewed} / {total} 页已审</span>
      <div className="submit-actions">
        {showCorrect ? (
          <>
            <button type="button" className="secondary" onClick={onCorrect}>
              正确并下一页
            </button>
            <button
              type="button"
              className="secondary issue"
              onClick={onMarkIssue}
              disabled={!canMarkIssue}
            >
              有问题并下一页
            </button>
          </>
        ) : null}
        <button type="button" className="primary" onClick={onSubmit} disabled={disabled}>
          提交给 Codex
        </button>
      </div>
    </footer>
  );
}
