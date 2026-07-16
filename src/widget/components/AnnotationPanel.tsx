interface AnnotationPanelProps {
  page: string;
  note: string;
  onNoteChange: (note: string) => void;
}

export function AnnotationPanel({ page, note, onNoteChange }: AnnotationPanelProps) {
  return (
    <aside className="annotation-panel">
      <p className="eyebrow">当前页批注</p>
      <h2>{page}</h2>
      <label htmlFor="annotation-note">哪里不对，应该怎么改？</label>
      <textarea
        id="annotation-note"
        value={note}
        onChange={(event) => onNoteChange(event.target.value)}
        placeholder="例如：同一男人肤色比前两页更深，保持原来的肤色、胡子和发型。"
        rows={8}
      />
      <p className="hint">圈画工具会保存为独立覆盖层，不会画进原图。</p>
      <details>
        <summary>高级信息</summary>
        <p>文件哈希、证据和规则范围默认隐藏，仅在阻塞时显示。</p>
      </details>
    </aside>
  );
}
