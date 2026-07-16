import { useMemo, useState } from "react";

import type {
  NormalizedAnnotationShape,
  PageReviewState,
  ReviewMode,
  RunPhase,
  SubmitAction,
} from "../shared/contracts";
import { deriveSubmitAction } from "../shared/stateMachine";
import { AnnotationPanel } from "./components/AnnotationPanel";
import { AnnotationCanvas } from "./components/AnnotationCanvas";
import { PageRail, type WorkbenchPage } from "./components/PageRail";
import { ProjectStart } from "./components/ProjectStart";
import { SubmitBar } from "./components/SubmitBar";

export interface WorkbenchInitialState {
  projectRoot: string;
  phase: RunPhase;
  pages: WorkbenchPage[];
  mode?: ReviewMode;
}

export interface WorkbenchApiLike {
  callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
}

interface AppProps {
  initialState: WorkbenchInitialState;
  api: WorkbenchApiLike;
}

export function App({ initialState, api }: AppProps) {
  const [mode, setMode] = useState<ReviewMode | undefined>(initialState.mode);
  const [phase, setPhase] = useState<RunPhase>(initialState.phase);
  const [running, setRunning] = useState(false);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [states, setStates] = useState<Record<string, PageReviewState>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [shapes, setShapes] = useState<Record<string, NormalizedAnnotationShape[]>>({});
  const reviewed = Object.values(states).filter((state) =>
    ["correct", "annotated", "passed", "needs_revision", "locked"].includes(state),
  ).length;
  const unreviewed = Math.max(0, initialState.pages.length - reviewed);
  const blockers =
    mode === "human_visual_auto_text" && phase === "input_review" && unreviewed
      ? [`${unreviewed} pages unreviewed`]
      : [];
  const dirty = mode !== undefined;
  const submitAction: SubmitAction = useMemo(() => {
    try {
      return deriveSubmitAction({ phase, dirty, running, blockers });
    } catch {
      return running ? "disabled_running" : "disabled_blocked";
    }
  }, [blockers, dirty, phase, running]);
  const page = initialState.pages[currentIndex];

  if (!mode || phase === "project_setup") {
    return (
      <ProjectStart
        projectRoot={initialState.projectRoot}
        onSelectMode={(selected) => {
          setMode(selected);
          setPhase("input_review");
        }}
      />
    );
  }
  if (phase === "processing") {
    return (
      <main className="processing-state">
        <span className="processing-dot" aria-hidden="true" />
        <h1>Codex 正在处理</h1>
        <p>状态来自 Skill 控制器；工作台不会显示虚假的百分比。</p>
      </main>
    );
  }

  const markCorrect = async (): Promise<void> => {
    if (!page) return;
    const next = { ...states, [page.path]: "correct" as const };
    setStates(next);
    await api.callTool("save_review_draft", {
      projectRoot: initialState.projectRoot,
      phase: "input_review",
      page: page.path,
      draft: { state: "correct", note: "", shapes: [] },
    });
    setCurrentIndex(Math.min(currentIndex + 1, initialState.pages.length - 1));
  };

  const markIssue = async (): Promise<void> => {
    if (!page) return;
    const pageNote = notes[page.path]?.trim() ?? "";
    const pageShapes = shapes[page.path] ?? [];
    if (!pageNote && pageShapes.length === 0) return;
    setStates({ ...states, [page.path]: "annotated" });
    await api.callTool("save_review_draft", {
      projectRoot: initialState.projectRoot,
      phase: "input_review",
      page: page.path,
      draft: { state: "annotated", note: pageNote, shapes: pageShapes },
    });
    setCurrentIndex(Math.min(currentIndex + 1, initialState.pages.length - 1));
  };

  const submit = async (): Promise<void> => {
    if (!mode || submitAction.startsWith("disabled_")) return;
    setRunning(true);
    await api.callTool("open_comic_review_workbench", {
      projectRoot: initialState.projectRoot,
      mode,
    });
    await api.callTool("submit_review_drafts", {
      projectRoot: initialState.projectRoot,
      phase: phase === "output_review" ? "output_review" : "input_review",
    });
    setPhase("processing");
    setRunning(false);
  };

  return (
    <main className="workbench-shell">
      <PageRail
        pages={initialState.pages}
        currentIndex={currentIndex}
        states={states}
        onSelect={setCurrentIndex}
      />
      <section className="canvas-column">
        <header className="canvas-toolbar">
          <strong>{mode === "automatic" ? "全自动模式" : "人工审查模式"}</strong>
          <span>适应窗口 · 100% · 放大</span>
        </header>
        <div className="canvas-placeholder">
          {page ? (
            <AnnotationCanvas
              page={page.path}
              sourceUrl={page.sourceUrl}
              shapes={shapes[page.path] ?? []}
              onShapesChange={(next) => setShapes({ ...shapes, [page.path]: next })}
            />
          ) : (
            <p>没有页面</p>
          )}
        </div>
      </section>
      {page ? (
        <AnnotationPanel
          page={page.path}
          note={notes[page.path] ?? ""}
          onNoteChange={(note) => setNotes({ ...notes, [page.path]: note })}
        />
      ) : null}
      <SubmitBar
        submitAction={submitAction}
        reviewed={reviewed}
        total={initialState.pages.length}
        showCorrect={mode === "human_visual_auto_text"}
        canMarkIssue={Boolean(
          page && ((notes[page.path]?.trim().length ?? 0) > 0 || (shapes[page.path]?.length ?? 0) > 0),
        )}
        onCorrect={() => void markCorrect()}
        onMarkIssue={() => void markIssue()}
        onSubmit={() => void submit()}
      />
    </main>
  );
}
