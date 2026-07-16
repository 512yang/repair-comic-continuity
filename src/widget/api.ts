import { App } from "@modelcontextprotocol/ext-apps";

import type { WorkbenchToolName } from "../server/tools";

export class WorkbenchApi {
  private readonly app = new App(
    { name: "漫画审校工作台", version: "1.0.0" },
    {},
    { autoResize: true, strict: true },
  );
  private connected = false;

  async connect(): Promise<void> {
    if (!this.connected) {
      await this.app.connect();
      this.connected = true;
    }
  }

  async callTool(
    name: WorkbenchToolName,
    args: Record<string, unknown>,
  ): Promise<unknown> {
    await this.connect();
    const result = await this.app.callServerTool({ name, arguments: args });
    return result.structuredContent ?? result;
  }
}
