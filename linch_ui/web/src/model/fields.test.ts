import { describe, expect, it } from "vitest";

import { FIELDS, PROJECT_FIELDS } from "./fields";

describe("v1alpha2 inspector field map", () => {
  it("maps runtime completion and shared safety limits to strict paths", () => {
    const keys = FIELDS.runtime_agent.map((field) => field.key);
    expect(keys).toContain("preset");
    expect(keys).toContain("completion/mode");
    expect(keys).toContain("completion/maxRetries");
    expect(keys).toContain("completion/verifiers");
    expect(keys).toContain("budget/maxTokens");
    expect(keys).toContain("tools");
  });

  it("does not expose worker-specific turn or budget fields", () => {
    const keys = FIELDS.subagent.map((field) => field.key);
    expect(keys).not.toContain("maxTurns");
    expect(keys.every((key) => !key.startsWith("budget/"))).toBe(true);
  });

  it("uses cron rather than the legacy nonexistent schedule field", () => {
    const keys = FIELDS.trigger.map((field) => field.key);
    expect(keys).toContain("cron");
    expect(keys).toContain("timezone");
    expect(keys).toContain("provider");
    expect(keys).toContain("signingSecretEnv");
    expect(keys).not.toContain("schedule");
  });

  it("exposes every editable composition relation through attachments or advanced fields", () => {
    expect(FIELDS.subagent.map((field) => field.key)).toContain("tools");
    expect(FIELDS.workflow_step.map((field) => field.key)).toEqual(
      expect.arrayContaining(["subagent", "tools", "dependsOn"]),
    );
    expect(FIELDS.routine.map((field) => field.key)).toContain("triggers");
    expect(FIELDS.skill.map((field) => field.key)).toContain("allowedTools");
  });

  it("assigns every field to a product inspector section", () => {
    const sections = new Set(
      Object.values(FIELDS).flatMap((fields) => fields.map((field) => field.section)),
    );
    expect(sections).toEqual(new Set(["Capabilities", "Quality", "Safety", "Lifecycle"]));
  });
});

describe("project configuration rail", () => {
  it("maps provider, memory, and MCP to real strict-model paths", () => {
    const keys = PROJECT_FIELDS.map((field) => field.key);
    expect(keys).toEqual([
      "runtime/provider/kind",
      "runtime/provider/model",
      "runtime/provider/apiKeyEnv",
      "runtime/provider/baseUrlEnv",
      "capabilities/memory/backend",
      "capabilities/memory/namespace",
      "capabilities/memory/dsnEnv",
      "capabilities/context/memoryRecall",
      "capabilities/memory/searchTool",
      "capabilities/memory/upsertTool",
      "capabilities/memory/extractionHook",
      "capabilities/extensions/mcpServers",
      "capabilities/extensions/liveMcpDiscovery",
    ]);
  });

  it("keeps secrets out of the Blueprint by naming env vars instead", () => {
    const env = PROJECT_FIELDS.filter((field) => field.env).map((field) => field.key);
    expect(env).toEqual([
      "runtime/provider/apiKeyEnv",
      "runtime/provider/baseUrlEnv",
      "capabilities/memory/dsnEnv",
    ]);
  });

  it("marks the extraction hook as a skeleton rather than working extraction", () => {
    const hook = PROJECT_FIELDS.find(
      (field) => field.key === "capabilities/memory/extractionHook",
    );
    expect(hook?.label).toContain("[td]");
    expect(hook?.desc).toMatch(/skeleton\/todo/);
  });

  it("describes MCP as a server rail, naming both transports", () => {
    const servers = PROJECT_FIELDS.find(
      (field) => field.key === "capabilities/extensions/mcpServers",
    );
    expect(servers?.type).toBe("json");
    expect(servers?.desc).toMatch(/not a Tool card/);
    expect(servers?.desc).toMatch(/http/);
    expect(servers?.desc).toMatch(/stdio/);
  });

  it("groups the rail under its own sections, never as canvas cards", () => {
    expect(new Set(PROJECT_FIELDS.map((field) => field.section))).toEqual(
      new Set(["Provider", "Memory", "MCP"]),
    );
  });
});
