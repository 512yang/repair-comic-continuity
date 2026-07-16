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

  it("saves a problem note and unlocks submit only after every page is reviewed", async () => {
    const api = { callTool: vi.fn(async () => ({ ok: true })) };
    render(<App initialState={humanProject} api={api} />);
    await userEvent.click(screen.getByRole("button", { name: "人工审查" }));

    const issue = screen.getByRole("button", { name: "有问题并下一页" });
    expect(issue).toBeDisabled();
    await userEvent.type(
      screen.getByLabelText("哪里不对，应该怎么改？"),
      "肤色和上一页不一致",
    );
    expect(issue).toBeEnabled();
    await userEvent.click(issue);
    await userEvent.click(screen.getByRole("button", { name: "正确并下一页" }));

    expect(screen.getByRole("button", { name: "提交给 Codex" })).toBeEnabled();
    expect(api.callTool).toHaveBeenCalledWith("save_review_draft", {
      projectRoot: "D:/demo",
      phase: "input_review",
      page: "0001.jpg",
      draft: {
        state: "annotated",
        note: "肤色和上一页不一致",
        shapes: [],
      },
    });
  });

  it("requires every output page once and keeps an unchanged passed page locked", async () => {
    const api = { callTool: vi.fn(async () => ({ ok: true })) };
    render(
      <App
        api={api}
        initialState={{
          projectRoot: "D:/demo",
          phase: "output_review",
          mode: "automatic",
          pages: [
            {
              path: "0001.jpg",
              sourceUrl: "data:image/jpeg;base64,source1",
              outputUrl: "data:image/jpeg;base64,output1",
              reviewState: "locked",
            },
            {
              path: "0002.jpg",
              sourceUrl: "data:image/jpeg;base64,source2",
              outputUrl: "data:image/jpeg;base64,output2",
              reviewState: "unreviewed",
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("1 / 2 页已审")).toBeVisible();
    expect(screen.getByRole("button", { name: "提交给 Codex" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "通过并下一页" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "通过并下一页" }));
    expect(api.callTool).toHaveBeenCalledWith("record_output_page_decision", {
      projectRoot: "D:/demo",
      page: "0002.jpg",
      decision: { state: "passed", note: "", shapes: [] },
    });
    expect(screen.getByRole("button", { name: "提交给 Codex" })).toBeEnabled();
  });
});
