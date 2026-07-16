// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App, type WorkbenchInitialState } from "../src/widget/App";

const humanProject: WorkbenchInitialState = {
  projectRoot: "D:/demo",
  phase: "project_setup",
  pages: [
    { path: "0001.jpg", sourceUrl: "file:///D:/demo/输入/0001.jpg" },
    { path: "0002.jpg", sourceUrl: "file:///D:/demo/输入/0002.jpg" },
  ],
};

afterEach(cleanup);

describe("comic review app shell", () => {
  it("starts with one simple mode choice and gates manual submission", async () => {
    const api = { callTool: vi.fn(async () => ({ ok: true })) };
    render(<App initialState={humanProject} api={api} />);
    expect(screen.getByRole("button", { name: "人工审查" })).toBeVisible();
    expect(screen.getByRole("button", { name: "全自动" })).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "人工审查" }));
    expect(screen.getByRole("button", { name: "正确并下一页" })).toBeVisible();
    expect(screen.getByRole("button", { name: "提交给 Codex" })).toBeDisabled();
    expect(screen.getByText("0 / 2 页已审")).toBeVisible();
  });

  it("lets automatic mode enter processing through the same submit button", async () => {
    const api = { callTool: vi.fn(async () => ({ ok: true })) };
    render(<App initialState={humanProject} api={api} />);
    await userEvent.click(screen.getByRole("button", { name: "全自动" }));
    expect(screen.queryByRole("button", { name: "正确并下一页" })).toBeNull();
    const submit = screen.getByRole("button", { name: "提交给 Codex" });
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    expect(api.callTool).toHaveBeenCalledWith("open_comic_review_workbench", {
      projectRoot: "D:/demo",
      mode: "automatic",
    });
    expect(screen.getByText("Codex 正在处理")).toBeVisible();
  });
});
