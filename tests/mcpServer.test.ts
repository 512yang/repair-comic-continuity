import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { afterEach, describe, expect, it } from "vitest";

import { APP_RESOURCE_URI } from "../mcp/server";
import { TOOL_NAMES } from "../src/server/tools";

let client: Client | undefined;
let projectRoot: string | undefined;
const serverRoot = process.env.PACKAGED_PLUGIN_ROOT ?? process.cwd();
const serverEntry = join(serverRoot, "dist", "mcp", "server.cjs");

afterEach(async () => {
  await client?.close();
  client = undefined;
  if (projectRoot) await rm(projectRoot, { recursive: true, force: true });
  projectRoot = undefined;
});

describe("built MCP server", () => {
  it("serves six tools and the single-file app resource over stdio", async () => {
    client = new Client({ name: "workbench-test-client", version: "1.0.0" });
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [serverEntry, "--stdio"],
      cwd: serverRoot,
      stderr: "pipe",
    });
    await client.connect(transport);

    const tools = await client.listTools();
    expect(tools.tools.map((tool) => tool.name)).toEqual(TOOL_NAMES);
    const resources = await client.listResources();
    expect(resources.resources.map((resource) => resource.uri)).toContain(
      APP_RESOURCE_URI,
    );
    const app = await client.readResource({ uri: APP_RESOURCE_URI });
    expect(app.contents[0]?.mimeType).toBe("text/html;profile=mcp-app");
    expect("text" in app.contents[0]! && app.contents[0].text).toContain(
      "漫画审校工作台",
    );
  });

  it("serves a sealed input page through an opaque session resource URI", async () => {
    projectRoot = await mkdtemp(join(tmpdir(), "comic-workbench-mcp-"));
    await mkdir(join(projectRoot, "输入"));
    const png = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII=",
      "base64",
    );
    await writeFile(join(projectRoot, "输入", "0001.png"), png);

    client = new Client({ name: "workbench-image-test", version: "1.0.0" });
    await client.connect(
      new StdioClientTransport({
        command: process.execPath,
        args: [serverEntry, "--stdio"],
        cwd: serverRoot,
        stderr: "pipe",
      }),
    );
    const opened = await client.callTool({
      name: "open_comic_review_workbench",
      arguments: { projectRoot, mode: "human_visual_auto_text" },
    });
    const payload = opened.structuredContent as {
      ok: true;
      data: { pages: Array<{ sourceUri: string }> };
    };
    const uri = payload.data.pages[0]!.sourceUri;
    expect(uri).toMatch(/^comic-page:\/\/[a-f0-9-]+\/input\/0$/);

    const image = await client.readResource({ uri });
    expect(image.contents[0]).toMatchObject({ uri, mimeType: "image/png" });
    expect("blob" in image.contents[0]! && image.contents[0].blob).toBe(
      png.toString("base64"),
    );
  });
});
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
