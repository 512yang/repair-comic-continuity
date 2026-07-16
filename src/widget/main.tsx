import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { WorkbenchApi } from "./api";
import "./styles.css";

const api = new WorkbenchApi();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App
      api={api}
      initialState={{ projectRoot: "", phase: "project_setup", pages: [] }}
    />
  </StrictMode>,
);
