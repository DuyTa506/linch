import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";

/**
 * Smoke flows against a real `linch-studio serve`. Project ids are unique per
 * test because the workspace is shared for the whole run.
 */

let seq = 0;
const nextId = () => `e2e_${Date.now().toString(36)}_${seq++}`;

async function createProject(
  page: Page,
  id: string,
  template: "Agent" | "Goal-verified" | "Directed workflow" | "Coordinator" | "Routine" =
    "Agent",
): Promise<void> {
  await page.goto("/");
  await page.getByRole("button", { name: "+ new blueprint" }).click();
  await page.getByRole("dialog").getByLabel("PROJECT ID").fill(id);
  await page.getByRole("dialog").getByLabel("TEMPLATE").selectOption({ label: template });
  await page.getByRole("button", { name: "create", exact: true }).click();
  await expect(page.locator(".topbar")).toContainText(`[${id}]`);
}

async function openProject(page: Page, id: string): Promise<void> {
  await page.goto("/");
  await page.locator(".home__trow", { hasText: id }).click();
  await expect(page.locator(".topbar")).toContainText(`[${id}]`);
}

/**
 * A default blueprint is deliberately *not* export-ready — it has no provider.
 * Fill one in through the API so export-path tests start from a valid project.
 */
async function giveProvider(request: APIRequestContext, id: string): Promise<void> {
  const doc = await (await request.get(`/api/v1/projects/${id}`)).json();
  const blueprint = parse(doc.yaml);
  blueprint.spec.runtime.provider.kind = "openai_responses";
  blueprint.spec.runtime.provider.model = "gpt-5";
  const put = await request.put(`/api/v1/projects/${id}/blueprint`, {
    data: { yaml: stringify(blueprint), baseDigest: doc.digest },
  });
  expect(put.ok()).toBe(true);
}

/** The saved digest, as the top bar renders it (truncated). */
async function digestChip(page: Page): Promise<string> {
  return (await page.locator(".topbar span", { hasText: /^digest / }).innerText()).trim();
}

async function dragCardToAttachment(page: Page, source: Locator, target: Locator): Promise<void> {
  const sourceBox = (await source.boundingBox())!;
  const targetBox = (await target.boundingBox())!;
  await page.mouse.move(sourceBox.x + sourceBox.width / 2, sourceBox.y + sourceBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    targetBox.x + targetBox.width / 2,
    targetBox.y - sourceBox.height / 2 - 8,
    { steps: 14 },
  );
  await expect(target).toHaveClass(/react-flow__node--magnetic-target/);
  await expect(page.locator(".react-flow__edge--preview")).toBeVisible();
  await page.mouse.up();
}

/**
 * Drag one workflow handle onto another to author control flow.
 *
 * Moves in steps like a real pointer: React Flow tracks a connection across
 * pointer moves, and a single jump to the target lands often enough to look
 * fine and fail intermittently.
 */
async function dragHandleToHandle(page: Page, source: Locator, target: Locator): Promise<void> {
  const sourceBox = (await source.boundingBox())!;
  const targetBox = (await target.boundingBox())!;
  await page.mouse.move(sourceBox.x + sourceBox.width / 2, sourceBox.y + sourceBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    targetBox.x + targetBox.width / 2,
    targetBox.y + targetBox.height / 2,
    { steps: 14 },
  );
  await expect(page.locator(".react-flow__connection")).toBeVisible();
  await page.mouse.up();
}

/**
 * Replace the editor contents by pasting. Typing YAML key-by-key would be
 * re-indented by CodeMirror and never reach the buffer verbatim.
 */
async function replaceEditor(page: Page, text: string): Promise<void> {
  const editor = page.getByTestId("yaml-editor").locator(".cm-content");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await editor.evaluate((element, value) => {
    const data = new DataTransfer();
    data.setData("text/plain", value);
    element.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }),
    );
  }, text);
}

test.describe("project lifecycle", () => {
  test("creates a project, then reopens it from the home table", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);

    await page.locator(".topbar__brand").click();
    const row = page.locator(".home__trow", { hasText: id });
    await expect(row).toBeVisible();
    // A fresh blueprint has no provider, so it must read as blocked, not ready.
    await expect(row).toContainText("blocked");

    await row.click();
    await expect(page.locator(".topbar")).toContainText(`[${id}]`);
    await expect(page.getByTestId("canvas")).toBeVisible();
  });

  test("shows the empty state when the workspace lists no projects", async ({ page }) => {
    await page.route("**/api/v1/projects", async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      await route.fulfill({ json: { projects: [] } });
    });
    await page.goto("/");

    await expect(page.getByText("no projects yet")).toBeVisible();
  });

  test("surfaces a boot failure instead of an empty shell", async ({ page }) => {
    await page.route("**/api/v1", (route) => route.abort("failed"));
    await page.goto("/");

    await expect(page.locator(".center")).toContainText("could not reach the studio server");
  });
});

test.describe("yaml editing", () => {
  test("reports diagnostics for a semantically invalid draft, and still saves it", async ({
    page,
  }) => {
    const id = nextId();
    await createProject(page, id);
    await giveProvider(page.request, id);
    await openProject(page, id);
    await page.getByRole("button", { name: "yaml", exact: true }).click();

    // Point the workflow output at a node that does not exist: structurally
    // parseable, semantically wrong.
    const doc = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    const blueprint = parse(doc.yaml);
    blueprint.spec.workflows = [
      {
        kind: "directed",
        id: "broken",
        displayName: "broken",
        output: "nope",
        nodes: [
          {
            type: "agent_call",
            id: "only",
            label: "only",
            prompt: "Do it.",
            dependsOn: [],
            subagent: null,
          },
        ],
      },
    ];
    await replaceEditor(page, stringify(blueprint));

    await expect(page.locator(".tabs")).toContainText(/[1-9]\d* err/, { timeout: 15_000 });
    await expect(page.locator(".tabs")).toContainText("export blocked");

    // A semantic-invalid draft is allowed onto disk.
    await page.getByRole("button", { name: "save", exact: true }).click();
    await expect(page.locator(".topbar")).toContainText("draft saved");

    await page.getByRole("button", { name: /diagnostics/ }).click();
    await expect(page.locator(".body")).toContainText("semantic.");
  });

  test("keeps structurally invalid yaml in the buffer and never writes it", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);
    const before = await digestChip(page);

    await page.getByRole("button", { name: "yaml", exact: true }).click();
    await replaceEditor(page, "this: [is: not: valid: yaml");

    await expect(page.locator(".topbar")).toContainText("unsaved buffer", { timeout: 15_000 });
    // Save must be unreachable, not merely discouraged.
    await expect(page.getByRole("button", { name: "save", exact: true })).toBeDisabled();
    expect(await digestChip(page)).toBe(before);

    await openProject(page, id);
    expect(await digestChip(page)).toBe(before);
  });
});

test.describe("canvas", () => {
  test("persists node positions without touching the digest", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);
    const before = await digestChip(page);

    const node = page.locator(".react-flow__node").first();
    await expect(node).toBeVisible();
    const box = (await node.boundingBox())!;

    const layoutWrite = page.waitForResponse(
      (response) => response.url().includes("/layout") && response.request().method() === "PUT",
    );
    await page.mouse.move(box.x + box.width / 2, box.y + 8);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 130, box.y + 100, { steps: 12 });
    await page.mouse.up();

    expect((await layoutWrite).ok()).toBe(true);
    // Layout is a separate document; it must never move the digest.
    expect(await digestChip(page)).toBe(before);

    // Assert against the stored layout rather than screen coordinates: the
    // canvas re-runs fitView on open, so on-screen position says nothing.
    const saved = await (await page.request.get(`/api/v1/projects/${id}/layout`)).json();
    expect(saved.layout.nodes).toHaveLength(1);
    const placed = saved.layout.nodes[0];

    // Reopening must not reset what the drag persisted.
    await openProject(page, id);
    await expect(page.locator(".react-flow__node")).toHaveCount(1);
    const reread = await (await page.request.get(`/api/v1/projects/${id}/layout`)).json();
    expect(reread.layout.nodes[0]).toEqual(placed);
    expect(await digestChip(page)).toBe(before);
  });

  test("adds a palette node, which does change the digest", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);
    const before = await digestChip(page);
    await expect(page.locator(".statusbar")).toContainText("1 nodes");

    await page.getByRole("button", { name: /subagent/ }).first().click();

    await expect(page.locator(".statusbar")).toContainText("2 nodes", { timeout: 15_000 });
    expect(await digestChip(page)).not.toBe(before);
  });

  test("magnetically attaches a Tool card to the primary or deep runtime", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);
    await page.getByRole("button", { name: "Agent loop" }).click();
    await page.locator('[data-id="primary_agent"]').click();
    const presetWrite = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await page.getByLabel("PRESET").selectOption("deep_agent");
    expect((await presetWrite).ok()).toBe(true);
    await page.getByRole("button", { name: /function tool/ }).first().click();

    const tool = page.locator('[data-id="tool_tool"]');
    const runtime = page.locator('[data-id="primary_agent"]');
    await expect(tool).toBeVisible();
    await expect(runtime).toBeVisible();
    const semanticWrite = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await dragCardToAttachment(page, tool, runtime);
    expect((await semanticWrite).ok()).toBe(true);

    // The attachment must announce itself: both endpoints pulse, the new edge is
    // badged with its relation, and a receipt says the edge outlived the drag.
    // React Flow briefly recreates its SVG wrappers while it measures a newly
    // dropped card. Locator assertions retry through that render, unlike a
    // one-shot DOM snapshot which can observe the wrapper between renders.
    const receipt = page.locator(".canvas__connection-confirmation");
    const edge = page.locator(".react-flow__edge--attach");
    await expect(receipt).toBeVisible();
    await expect(receipt).toHaveAttribute("role", "status");
    await expect(receipt).toContainText("TOOL ACCESS");
    await expect(tool).toHaveClass(/react-flow__node--attachment-saved/);
    await expect(runtime).toHaveClass(/react-flow__node--attachment-saved/);
    await expect(edge).toHaveClass(/react-flow__edge--attachment-saved/);
    await expect(page.locator(".react-flow__edge-text")).toHaveText("TOOL ACCESS");

    // Transient feedback expires; the attachment itself does not.
    await expect(receipt).toBeHidden({ timeout: 8_000 });
    await expect(tool).not.toHaveClass(/react-flow__node--attachment-saved/);
    await expect(page.locator(".react-flow__edge--attach")).toHaveCount(1);

    const saved = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    expect(parse(saved.yaml).spec.runtime.agent).toMatchObject({
      preset: "deep_agent",
      tools: ["tool"],
    });
    await openProject(page, id);
    await page.getByRole("button", { name: "Agent loop" }).click();
    await expect(page.locator(".react-flow__edge--attach")).toHaveCount(1);
  });

  test("declares a Tool unattached and attaches it explicitly from the inspector", async ({
    page,
  }) => {
    const id = nextId();
    await createProject(page, id);
    await page.getByRole("button", { name: "Agent loop" }).click();
    await page.getByRole("button", { name: /function tool/ }).first().click();

    // Adding a card must not silently wire it up: the template pins an empty
    // allowlist, so the tool is declared and genuinely detached.
    await expect(page.getByTestId("toast")).toContainText("not attached yet");
    const attachment = page.getByTestId("tool-attachment");
    await expect(attachment).toContainText("Attach to primary agent");
    await expect(page.locator(".react-flow__edge--attach")).toHaveCount(0);

    const write = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await attachment.getByRole("button", { name: "Attach to primary agent" }).click();
    expect((await write).ok()).toBe(true);

    await expect(attachment).toContainText("attached to primary agent");
    await expect(page.locator(".react-flow__edge--attach")).toHaveCount(1);
    const saved = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    expect(parse(saved.yaml).spec.runtime.agent.tools).toEqual(["tool"]);
  });

  test("says what a Subagent and a Skill are, without calling either a Tool", async ({ page }) => {
    await createProject(page, nextId());
    await page.getByRole("button", { name: "Agent loop" }).click();

    await page.getByRole("button", { name: /subagent/ }).first().click();
    await expect(page.getByTestId("toast")).toContainText("runtime member");
    await page.locator('[data-id="subagent_subagent"]').click();
    await expect(page.getByTestId("subagent-note")).toContainText(
      "bind to a workflow step for execution",
    );

    await page.getByRole("button", { name: /skill/ }).first().click();
    await expect(page.getByTestId("toast")).toContainText("SKILL.md");
    await page.locator('[data-id="skill_skill"]').click();
    await expect(page.getByTestId("skill-note")).toContainText("restrict allowedTools");

    // Neither declaration invents an edge to the runtime agent.
    await expect(page.locator(".react-flow__edge")).toHaveCount(0);
  });

  test("binds a cron Trigger to a Routine on the canvas", async ({ page }) => {
    const id = nextId();
    await createProject(page, id, "Goal-verified");
    await page.getByRole("button", { name: "Agent loop" }).click();
    await page.getByRole("button", { name: /agent tick routine/ }).click();
    const routine = page.locator('.react-flow__node[data-id^="loop_"]').first();
    await expect(routine).toBeVisible();

    const triggerWrite = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: /cron trigger/ }).click();
    expect((await triggerWrite).ok()).toBe(true);

    const bindingWrite = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await dragCardToAttachment(page, page.locator('[data-id="trigger_cron"]'), routine);
    expect((await bindingWrite).ok()).toBe(true);

    const saved = parse((await (await page.request.get(`/api/v1/projects/${id}`)).json()).yaml);
    expect(saved.spec.routines.at(-1).triggers).toContain("cron");
  });

  test("hands the inspector to a new Tool even when another card was selected", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);
    await page.getByRole("button", { name: "Agent loop" }).click();
    // Configuring the agent first is the normal way in; adding a Tool afterwards
    // must still surface that Tool's attachment control, not keep the agent.
    await page.locator('[data-id="primary_agent"]').click();
    await expect(page.getByTestId("runtime-summary")).toBeVisible();

    const write = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: /function tool/ }).first().click();
    expect((await write).ok()).toBe(true);

    await expect(page.getByTestId("tool-attachment")).toBeVisible();
    await expect(page.getByTestId("runtime-summary")).toBeHidden();
  });

  test("configures provider, memory, and MCP as a rail that creates no edge", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);
    // The project map draws no card for runtime wiring, so the rail stands in
    // for an empty selection there.
    const rail = page.getByTestId("project-rail");
    await expect(rail).toBeVisible();
    const edgesBefore = await page.locator(".react-flow__edge").count();

    // Every field writes straight to disk, so settle each save before the next
    // edit rather than racing compare-and-swap against ourselves.
    const settle = async (edit: () => Promise<unknown>) => {
      const write = page.waitForResponse(
        (response) =>
          response.url().includes("/blueprint") && response.request().method() === "PUT",
      );
      await edit();
      expect((await write).ok()).toBe(true);
    };

    await settle(() => rail.getByLabel("PROVIDER").selectOption("anthropic"));
    await settle(async () => {
      await rail.getByLabel("API KEY ENV").fill("ANTHROPIC_API_KEY");
      await rail.getByLabel("API KEY ENV").blur();
    });
    await settle(() => rail.getByLabel("MEMORY BACKEND").selectOption("sqlite"));
    await settle(async () => {
      await rail.getByLabel("DSN ENV").fill("MEMORY_DSN");
      await rail.getByLabel("DSN ENV").blur();
    });
    await settle(() => rail.getByLabel("RECALL INJECTION").selectOption("true"));
    await settle(() => rail.getByLabel("SEARCH TOOL").selectOption("true"));
    await settle(async () => {
      await rail
        .getByLabel("MCP SERVERS")
        .fill(
          '[{"kind":"http","id":"docs","url":"https://mcp.example.com","tokenEnv":"MCP_TOKEN"}]',
        );
      await rail.getByLabel("MCP SERVERS").blur();
    });
    await settle(() => rail.getByLabel("LIVE DISCOVERY").selectOption("true"));

    await expect(page.getByTestId("rail-receipt")).toContainText("no graph edge was created");
    expect(await page.locator(".react-flow__edge").count()).toBe(edgesBefore);

    const saved = parse((await (await page.request.get(`/api/v1/projects/${id}`)).json()).yaml);
    expect(saved.spec.runtime.provider).toMatchObject({
      kind: "anthropic",
      apiKeyEnv: "ANTHROPIC_API_KEY",
    });
    expect(saved.spec.capabilities.memory).toMatchObject({
      backend: "sqlite",
      dsnEnv: "MEMORY_DSN",
      searchTool: true,
    });
    expect(saved.spec.capabilities.context.memoryRecall).toBe(true);
    expect(saved.spec.capabilities.extensions).toMatchObject({
      liveMcpDiscovery: true,
      mcpServers: [
        { kind: "http", id: "docs", url: "https://mcp.example.com", tokenEnv: "MCP_TOKEN" },
      ],
    });

    // The rail is real configuration: it survives a reload like any other edit.
    await openProject(page, id);
    await expect(page.getByTestId("project-rail").getByLabel("MEMORY BACKEND")).toHaveValue(
      "sqlite",
    );
  });

  test("explains why a Subagent card cannot form a direct runtime A2A edge", async ({ page }) => {
    await createProject(page, nextId());
    await page.getByRole("button", { name: /subagent/ }).first().click();
    await page.getByRole("button", { name: "Agent loop" }).click();

    const subagent = page.locator('[data-id="subagent_subagent"]');
    const runtime = page.locator('[data-id="primary_agent"]');
    await expect(subagent.locator(".node__handle--attach-out")).toHaveClass(
      /node__handle--disabled/,
    );
    const sourceBox = (await subagent.boundingBox())!;
    const targetBox = (await runtime.boundingBox())!;
    await page.mouse.move(sourceBox.x + sourceBox.width / 2, sourceBox.y + sourceBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(
      targetBox.x + targetBox.width / 2,
      targetBox.y - sourceBox.height / 2 - 8,
      { steps: 12 },
    );
    await page.mouse.up();

    await expect(page.getByText(/already belongs to the project runtime/i)).toBeVisible();
    await expect(page.locator(".react-flow__edge--attach")).toHaveCount(0);
  });

  test("builds bound multi-agent steps and A2A control flow inside a workflow", async ({ page }) => {
    const id = nextId();
    await createProject(page, id, "Directed workflow");
    await page.getByRole("button", { name: /Workflow · Main Workflow/ }).click();
    await page.getByRole("button", { name: /subagent/ }).first().click();
    await expect(page.locator('[data-id="wf_main_workflow_subagent_step"]')).toBeVisible();
    await page.getByRole("button", { name: /subagent/ }).first().click();
    await expect(page.locator('[data-id="wf_main_workflow_subagent_2_step"]')).toBeVisible();

    const source = page.locator(
      '[data-id="wf_main_workflow_subagent_step"] .node__handle--out',
    );
    const target = page.locator(
      '[data-id="wf_main_workflow_subagent_2_step"] .node__handle--in',
    );
    const semanticWrite = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await dragHandleToHandle(page, source, target);
    expect((await semanticWrite).ok()).toBe(true);

    const saved = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    const workflow = parse(saved.yaml).spec.workflows[0];
    expect(workflow.nodes.at(-2)).toMatchObject({ subagent: "subagent" });
    expect(workflow.nodes.at(-1)).toMatchObject({
      subagent: "subagent_2",
      dependsOn: ["subagent_step"],
    });
  });

  test("composes workflow → routine → cron and opens the outside Routine scope", async ({
    page,
  }) => {
    const id = nextId();
    await createProject(page, id, "Directed workflow");
    await page.getByRole("button", { name: /Workflow · Main Workflow/ }).click();
    await page.getByRole("button", { name: /scheduled workflow/ }).click();

    await expect(page.getByRole("button", { name: /Routine · Scheduled workflow/ })).toHaveClass(
      /scopebar__item--active/,
    );
    await expect(page.locator('[data-id="trigger_every_2h"]')).toBeVisible();
    await expect(page.locator('[data-id="loop_scheduled_workflow"]')).toBeVisible();
    await expect(page.locator('[data-id="workflow_main_workflow"]')).toBeVisible();
    await expect(page.locator(".react-flow__edge--attach")).toHaveCount(2);
    await page.locator('[data-id="trigger_every_2h"]').click();
    await expect(page.getByTestId("inspector")).toContainText("CRON");
    await expect(page.getByLabel("CRON")).toHaveValue("0 */2 * * *");

    const saved = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    const blueprint = parse(saved.yaml);
    expect(blueprint.spec.triggers.at(-1)).toMatchObject({
      kind: "cron",
      cron: "0 */2 * * *",
      timezone: "UTC",
    });
    expect(blueprint.spec.routines.at(-1)).toMatchObject({
      kind: "workflow_run",
      target: "main_workflow",
      triggers: ["every_2h"],
    });
  });

  test("removes the selected node with Del", async ({ page }) => {
    await createProject(page, nextId());
    await page.getByRole("button", { name: /subagent/ }).first().click();
    await expect(page.locator(".statusbar")).toContainText("2 nodes", { timeout: 15_000 });

    await page.getByRole("button", { name: "Agent loop" }).click();
    await page.locator('[data-id="subagent_subagent"]').click();
    await expect(page.getByTestId("inspector")).toBeVisible();
    await page.keyboard.press("Delete");

    await expect(page.locator(".statusbar")).toContainText("1 nodes", { timeout: 15_000 });
  });

  test("connects compatible workflow ports and persists dependsOn", async ({ page }) => {
    const id = nextId();
    await createProject(page, id, "Directed workflow");
    await page.getByRole("button", { name: /Workflow · Main Workflow/ }).click();
    const agentCall = page.getByRole("button", { name: /agent-call step/ }).first();

    // The template supplies a two-step graph; add a third step in this explicit scope.
    const paletteWrite = page.waitForResponse(
      (response) => response.url().includes("/blueprint") && response.request().method() === "PUT",
    );
    await agentCall.click();
    expect((await paletteWrite).ok()).toBe(true);
    const steps = page.locator(".react-flow__node:has(.node--connectable)");
    await expect(steps).toHaveCount(3, { timeout: 15_000 });

    const source = page.locator('[data-id="wf_main_workflow_produce"] .node__handle--out');
    const target = page.locator('[data-id="wf_main_workflow_step"] .node__handle--in');
    await expect(source).toBeVisible();
    await expect(target).toBeVisible();
    await dragHandleToHandle(page, source, target);

    await expect(page.locator(".statusbar")).toContainText("2 edges", { timeout: 15_000 });
    const doc = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    const nodes = doc.blueprint.spec.workflows[0].nodes;
    expect(nodes.find((node: { id: string }) => node.id === "step").dependsOn).toContain("produce");

    // Adding a step advances the terminal, so the new output deliberately has
    // no outgoing port while the former terminal becomes connectable.
    await expect(
      page.locator('[data-id="wf_main_workflow_step"] .node__handle--out'),
    ).not.toBeVisible();

    const createdEdge = page.getByTestId(
      "rf__edge-wf_main_workflow_produce__wf_main_workflow_step__depends_on",
    );
    await createdEdge.focus();
    await page.keyboard.press("Enter");
    await page.getByRole("button", { name: "Delete edge" }).click();
    await expect(page.locator(".statusbar")).toContainText("1 edges", { timeout: 15_000 });
    await page.getByRole("button", { name: "Undo" }).click();
    await expect(page.locator(".statusbar")).toContainText("2 edges", { timeout: 15_000 });
  });

  test("navigates explicit scopes and exposes goal verification fields", async ({ page }) => {
    await createProject(page, nextId(), "Goal-verified");
    await expect(page.getByRole("button", { name: "Project map" })).toBeVisible();
    await page.getByRole("button", { name: "Agent loop" }).click();
    await page.locator(".react-flow__node").first().click();
    await expect(page.getByTestId("inspector")).toContainText("COMPLETION");
    await expect(page.getByTestId("inspector")).toContainText("verifier_gated");
  });

  test("never offers an unsupported capability as a draggable node", async ({ page }) => {
    await createProject(page, nextId());

    // Unsupported capabilities are listed for honesty, but must not be addable:
    // no button to click, and nothing to drag onto the canvas.
    const unsupported = page.locator(".palette__item", { hasText: "Condition nodes" });
    await expect(unsupported).toBeVisible();
    await expect(unsupported).not.toHaveAttribute("draggable", "true");
    await expect(page.getByRole("button", { name: "Condition nodes" })).toHaveCount(0);
  });
});

test.describe("stale save", () => {
  test("shows the conflict banner when the digest moved underneath, and recovers", async ({
    page,
  }) => {
    const id = nextId();
    await createProject(page, id);

    // Write behind the UI's back so its baseDigest goes stale.
    await giveProvider(page.request, id);

    await page.getByRole("button", { name: "yaml", exact: true }).click();
    const doc = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    const blueprint = parse(doc.yaml);
    blueprint.metadata.description = "written from a stale buffer";
    await replaceEditor(page, stringify(blueprint));
    await page.getByRole("button", { name: "save", exact: true }).click();

    const banner = page.getByRole("alert");
    await expect(banner).toContainText("save conflict", { timeout: 15_000 });

    // Reverting to disk clears the conflict rather than clobbering the other write.
    await banner.getByRole("button", { name: "revert" }).click();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.locator(".topbar")).not.toContainText("save conflict");
  });
});

test.describe("export", () => {
  test("previews the generated files for a valid blueprint", async ({ page }) => {
    const id = nextId();
    await createProject(page, id);
    await giveProvider(page.request, id);
    await openProject(page, id);

    await page.getByRole("button", { name: "files", exact: true }).click();

    await expect(page.locator(".files__tree")).toContainText("agent.py", { timeout: 20_000 });
    await page.locator(".files__row", { hasText: "agent.py" }).first().click();
    await expect(page.locator(".files__view")).toContainText("sha256");
    await expect(page.locator(".files__code")).toContainText(/import|def /);
  });

  test("blocks export while errors exist and never offers overwrite", async ({ page }) => {
    // A fresh project has no provider, so it is genuinely export-blocked.
    await createProject(page, nextId());
    await expect(page.locator(".tabs")).toContainText("export blocked");

    await page.getByRole("button", { name: "export", exact: true }).click();

    await expect(page.locator(".body")).toContainText("NO OVERWRITE");
    await expect(page.locator(".body")).toContainText("2 blocking error(s)");

    // The copy explains that force/regenerate do not exist, so assert on the
    // controls: no button may offer overwrite, regenerate, or import-back.
    await expect(
      page.locator(".body").getByRole("button", { name: /overwrite|regenerate|force|import/i }),
    ).toHaveCount(0);
  });
});

test.describe("support drawer", () => {
  test("keeps deterministic pipeline authoring available without a provider", async ({ page }) => {
    await createProject(page, nextId());
    await expect(page.locator(".topbar")).toContainText("[ai: off]");

    await page.getByRole("button", { name: /support/i }).click();

    const drawer = page.getByRole("dialog", { name: "Linch support" });
    await expect(drawer).toContainText("docs · implementation · pipelines");
    await expect(drawer).toContainText("pipeline work needs a confirmation");
    // Provider-backed docs are unavailable, but deterministic pipeline planning
    // deliberately remains usable from the same Support surface.
    await expect(drawer.getByRole("textbox")).toHaveAttribute(
      "placeholder",
      /configure LINCH_STUDIO_PROVIDER/,
    );
  });
});
