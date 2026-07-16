import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  RESOURCE_MIME_TYPE,
  registerAppResource,
  registerAppTool,
} from "@modelcontextprotocol/ext-apps/server";
import { z } from "zod/v4";

import { commitNormalizedWithPython } from "../src/server/pythonCommit";
import {
  createWorkbenchToolService,
  type ToolCallResult,
  type WorkbenchToolName,
} from "../src/server/tools";

export const APP_RESOURCE_URI = "ui://comic-review-workbench/index.html";

export function createMcpServer(): McpServer {
  const server = new McpServer({
    name: "repair-comic-continuity-workbench",
    version: "1.0.0",
  });
  const tools = createWorkbenchToolService({
    commitNormalized: commitNormalizedWithPython,
  });
  const invoke = async (
    name: WorkbenchToolName,
    input: Record<string, unknown>,
  ) => toolResult(await tools.callTool(name, input));

  registerAppResource(
    server,
    "漫画审校工作台",
    APP_RESOURCE_URI,
    { description: "逐页圈画、批注、提交和复核漫画修复结果。" },
    async () => ({
      contents: [
        {
          uri: APP_RESOURCE_URI,
          mimeType: RESOURCE_MIME_TYPE,
          text: await readFile(resolve("dist", "widget", "index.html"), "utf8"),
          _meta: { ui: { prefersBorder: true } },
        },
      ],
    }),
  );

  registerAppTool(
    server,
    "open_comic_review_workbench",
    {
      title: "打开漫画审校工作台",
      description: "打开或恢复本地漫画审校项目，并锁定项目文件清单。",
      inputSchema: {
        projectRoot: z.string().min(1),
        mode: z.enum(["human_visual_auto_text", "automatic"]),
      },
      annotations: { readOnlyHint: false },
      _meta: {
        ui: { resourceUri: APP_RESOURCE_URI, visibility: ["model", "app"] },
      },
    },
    async (input) => invoke("open_comic_review_workbench", input),
  );
  registerAppTool(
    server,
    "save_review_draft",
    {
      title: "保存审校草稿",
      description: "只保存一页的圈画、批注或页面判断，不修改图片。",
      inputSchema: {
        projectRoot: z.string().min(1),
        phase: z.enum(["input_review", "output_review"]),
        page: z.string().min(1),
        draft: z.record(z.string(), z.unknown()),
      },
      _meta: { ui: { resourceUri: APP_RESOURCE_URI, visibility: ["app"] } },
    },
    async (input) => invoke("save_review_draft", input),
  );
  registerAppTool(
    server,
    "submit_review_drafts",
    {
      title: "提交给 Codex",
      description: "提交原始圈画和用户批注；不推断缺陷代码或修复状态。",
      inputSchema: {
        projectRoot: z.string().min(1),
        phase: z.enum(["input_review", "output_review"]),
      },
      _meta: {
        ui: { resourceUri: APP_RESOURCE_URI, visibility: ["model", "app"] },
      },
    },
    async (input) => invoke("submit_review_drafts", input),
  );
  registerAppTool(
    server,
    "commit_normalized_annotations",
    {
      title: "提交结构化审校证据",
      description: "仅在 Codex 补全结构化字段后调用 Python 验证器并写入运行证据。",
      inputSchema: {
        projectRoot: z.string().min(1),
        runRelativePath: z.string().min(1),
        selectionDocument: z.unknown(),
        annotationDocument: z.unknown().nullable(),
        revisionDocument: z.unknown().nullable(),
      },
      _meta: { ui: { resourceUri: APP_RESOURCE_URI, visibility: ["model"] } },
    },
    async (input) => invoke("commit_normalized_annotations", input),
  );
  registerAppTool(
    server,
    "get_comic_review_status",
    {
      title: "读取漫画审校状态",
      description: "读取控制器与证据文件推导出的真实进度和阻塞项。",
      inputSchema: { projectRoot: z.string().min(1) },
      annotations: { readOnlyHint: true },
      _meta: {
        ui: { resourceUri: APP_RESOURCE_URI, visibility: ["model", "app"] },
      },
    },
    async (input) => invoke("get_comic_review_status", input),
  );
  registerAppTool(
    server,
    "record_output_page_decision",
    {
      title: "保存输出页复核",
      description: "保存输出页通过或需二修的判断及候选哈希。",
      inputSchema: {
        projectRoot: z.string().min(1),
        page: z.string().min(1),
        decision: z.record(z.string(), z.unknown()),
      },
      _meta: { ui: { resourceUri: APP_RESOURCE_URI, visibility: ["app"] } },
    },
    async (input) => invoke("record_output_page_decision", input),
  );
  return server;
}

function toolResult(result: ToolCallResult) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(result) }],
    structuredContent: result as unknown as Record<string, unknown>,
    isError: !result.ok,
  };
}

export async function startStdioServer(): Promise<void> {
  const server = createMcpServer();
  await server.connect(new StdioServerTransport());
}

if (process.argv.includes("--stdio")) {
  startStdioServer().catch((error: unknown) => {
    process.stderr.write(
      `comic review workbench MCP server failed: ${
        error instanceof Error ? error.message : String(error)
      }\n`,
    );
    process.exitCode = 1;
  });
}
