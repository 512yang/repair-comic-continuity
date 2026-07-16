import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { App, type WorkbenchInitialState } from "./App";
import { WorkbenchApi } from "./api";
import { buildInitialStateFromToolResult } from "./bootstrap";
import "./styles.css";

const api = new WorkbenchApi();

function WorkbenchRoot() {
  const [initialState, setInitialState] = useState<WorkbenchInitialState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const unsubscribe = api.onToolResult((result) => {
      void buildInitialStateFromToolResult(result, (uri) => api.readImageResource(uri))
        .then((next) => {
          if (next) setInitialState(next);
        })
        .catch((cause: unknown) =>
          setError(cause instanceof Error ? cause.message : String(cause)),
        );
    });
    void api.connect().catch((cause: unknown) =>
      setError(cause instanceof Error ? cause.message : String(cause)),
    );
    return unsubscribe;
  }, []);

  if (error) {
    return (
      <main className="processing-state error-state">
        <h1>工作台无法打开</h1>
        <p>{error}</p>
      </main>
    );
  }
  if (!initialState) {
    return (
      <main className="processing-state">
        <span className="processing-dot" aria-hidden="true" />
        <h1>正在读取项目</h1>
        <p>等待 Codex 返回已封存的页面清单。</p>
      </main>
    );
  }
  return <App api={api} initialState={initialState} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <WorkbenchRoot />
  </StrictMode>,
);
