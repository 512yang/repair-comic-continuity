import type { ReviewMode } from "../../shared/contracts";

interface ProjectStartProps {
  projectRoot: string;
  onSelectMode: (mode: ReviewMode) => void;
}

export function ProjectStart({ projectRoot, onSelectMode }: ProjectStartProps) {
  return (
    <section className="project-start" aria-labelledby="project-start-title">
      <p className="eyebrow">新建审校运行</p>
      <h1 id="project-start-title">选择处理模式</h1>
      <p className="project-path" title={projectRoot}>
        {projectRoot || "请先从 Codex 选择漫画项目"}
      </p>
      <div className="mode-grid">
        <button
          type="button"
          aria-label="人工审查"
          className="mode-card mode-card-primary"
          onClick={() => onSelectMode("human_visual_auto_text")}
        >
          <strong>人工审查</strong>
          <span>逐页判断画面；文字仍由内嵌引擎全量检查</span>
        </button>
        <button
          type="button"
          aria-label="全自动"
          className="mode-card"
          onClick={() => onSelectMode("automatic")}
        >
          <strong>全自动</strong>
          <span>Codex 检查画面；完成后仍逐页人工复核输出</span>
        </button>
      </div>
    </section>
  );
}
