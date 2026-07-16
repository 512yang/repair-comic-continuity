import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { afterEach, describe, expect, it } from "vitest";

import { APP_RESOURCE_URI } from "../mcp/server";
import { TOOL_NAMES } from "../src/server/tools";

let client: Client | undefined;

afterEach(async () => {
  await client?.close();
  client = undefined;
});

describe("built MCP server", () => {
  it("serves six tools and the single-file app resource over stdio", async () => {
    client = new Client({ name: "workbench-test-client", version: "1.0.0" });
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ["dist/mcp/server.cjs", "--stdio"],
      cwd: process.cwd(),
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
});
