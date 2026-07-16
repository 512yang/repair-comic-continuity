import { App } from "@modelcontextprotocol/ext-apps";

import type { WorkbenchToolName } from "../server/tools";

type ToolResultListener = (result: unknown) => void;

export class WorkbenchApi {
  private readonly app = new App(
    { name: "漫画审校工作台", version: "1.0.0" },
    {},
    { autoResize: true, strict: true },
  );
  private connected = false;
  private readonly toolResultListeners = new Set<ToolResultListener>();

  constructor() {
    this.app.addEventListener("toolresult", (result) => {
      for (const listener of this.toolResultListeners) listener(result);
    });
  }

  onToolResult(listener: ToolResultListener): () => void {
    this.toolResultListeners.add(listener);
    return () => this.toolResultListeners.delete(listener);
  }

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

  async readImageResource(uri: string): Promise<string> {
    await this.connect();
    const result = await this.app.readServerResource({ uri });
    const content = result.contents[0];
    if (!content || !("blob" in content) || typeof content.blob !== "string") {
      throw new Error(`Image resource did not return binary data: ${uri}`);
    }
    const mimeType = content.mimeType ?? "application/octet-stream";
    return `data:${mimeType};base64,${content.blob}`;
  }
}
