import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("plugin manifest", () => {
  it("binds the embedded skill and local MCP server", () => {
    expect(existsSync("skills/repair-comic-continuity/SKILL.md")).toBe(true);
    const manifest = JSON.parse(
      readFileSync(".codex-plugin/plugin.json", "utf8"),
    );
    expect(manifest.name).toBe("repair-comic-continuity-workbench");
    expect(manifest.skills).toBe("./skills/");
    expect(manifest.mcpServers).toBe("./.mcp.json");
    expect(existsSync(".mcp.json")).toBe(true);
  });
});
