import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { once } from "node:events";

import { chromium } from "@playwright/test";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = resolve(SCRIPT_DIR, "..");
const SCREENSHOT_DIR = resolve(WEB_ROOT, "src/assets/documentation");
const VIEWPORT = { width: 1440, height: 900 };
const SERVER_TIMEOUT_MS = 30_000;
/** Pauses that make a recording readable; screenshots do not use them. */
const BEAT_MS = 900;

function studioBinary() {
  if (process.env.LINCH_STUDIO_BIN) return process.env.LINCH_STUDIO_BIN;
  const localBinary = resolve(WEB_ROOT, "../.venv/bin/linch-studio");
  return existsSync(localBinary) ? localBinary : "linch-studio";
}

async function availablePort() {
  const probe = createServer();
  probe.unref();
  await new Promise((resolveListen, rejectListen) => {
    probe.once("error", rejectListen);
    probe.listen(0, "127.0.0.1", resolveListen);
  });
  const address = probe.address();
  if (!address || typeof address === "string") {
    probe.close();
    throw new Error("Could not reserve a local port for linch-studio.");
  }
  const port = address.port;
  await new Promise((resolveClose, rejectClose) => {
    probe.close((error) => (error ? rejectClose(error) : resolveClose()));
  });
  return port;
}

async function waitForServer(baseUrl, server, output) {
  const deadline = Date.now() + SERVER_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (output.startError) throw output.startError;
    if (server.exitCode !== null) {
      throw new Error(
        `linch-studio exited with code ${server.exitCode} before it became ready.\n${output.text}`,
      );
    }
    try {
      const response = await fetch(`${baseUrl}/api/v1`);
      if (response.ok) return;
    } catch {
      // The socket is expected to refuse connections while uvicorn starts.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`Timed out waiting for linch-studio at ${baseUrl}.\n${output.text}`);
}

async function stopServer(server) {
  if (server.exitCode !== null) return;
  server.kill("SIGTERM");
  const exited = once(server, "exit");
  const timeout = new Promise((resolveTimeout) => setTimeout(resolveTimeout, 5_000, false));
  if (!(await Promise.race([exited.then(() => true), timeout]))) {
    server.kill("SIGKILL");
    await once(server, "exit");
  }
}

async function expectOk(responsePromise, description) {
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`${description} failed with HTTP ${response.status()}: ${response.url()}`);
  }
}

function blueprintWrite(page) {
  return page.waitForResponse(
    (response) =>
      response.url().includes("/blueprint") && response.request().method() === "PUT",
  );
}

async function createProject(page, id, template) {
  await page.goto("/");
  await page.getByRole("button", { name: "+ new blueprint" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("PROJECT ID").fill(id);
  await dialog.getByLabel("TEMPLATE").selectOption({ label: template });
  await page.getByRole("button", { name: "create", exact: true }).click();
  await page.locator(".topbar", { hasText: `[${id}]` }).waitFor();
}

/** Same flow as `createProject`, driven by a pointer a viewer can follow. */
async function createProjectOnCamera(page, id, template) {
  await page.goto("/");
  await beat(page);
  await clickAt(page, page.getByRole("button", { name: "+ new blueprint" }));
  const dialog = page.getByRole("dialog");
  await typeInto(page, dialog.getByLabel("PROJECT ID"), id);
  await chooseAt(page, dialog.getByLabel("TEMPLATE"), { label: template });
  await clickAt(page, page.getByRole("button", { name: "create", exact: true }));
  await page.locator(".topbar", { hasText: `[${id}]` }).waitFor();
  await beat(page);
}

async function dragCardToAttachment(page, source, target) {
  const sourceBox = await source.boundingBox();
  const targetBox = await target.boundingBox();
  if (!sourceBox || !targetBox) throw new Error("Cannot attach cards that are not visible.");

  await page.mouse.move(sourceBox.x + sourceBox.width / 2, sourceBox.y + sourceBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    targetBox.x + targetBox.width / 2,
    targetBox.y - sourceBox.height / 2 - 8,
    { steps: 14 },
  );
  await page.locator(".react-flow__edge--preview").waitFor();
  await page.mouse.up();
}

/** Drag one workflow handle onto another, moving like a real pointer. */
async function dragHandleToHandle(page, source, target) {
  const sourceBox = await source.boundingBox();
  const targetBox = await target.boundingBox();
  if (!sourceBox || !targetBox) throw new Error("Cannot connect handles that are not visible.");

  await page.mouse.move(sourceBox.x + sourceBox.width / 2, sourceBox.y + sourceBox.height / 2, {
    steps: 18,
  });
  await page.mouse.down();
  await page.mouse.move(targetBox.x + targetBox.width / 2, targetBox.y + targetBox.height / 2, {
    steps: 18,
  });
  await page.locator(".react-flow__connection").waitFor();
  await page.mouse.up();
}

async function settleForScreenshot(page) {
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  });
  await page.evaluate(async () => {
    await document.fonts.ready;
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  });
  await page.mouse.move(VIEWPORT.width - 8, VIEWPORT.height - 8);
  await page.waitForTimeout(200);
}

async function capture(page, filename) {
  await settleForScreenshot(page);
  const path = join(SCREENSHOT_DIR, filename);
  await page.screenshot({ path, fullPage: true });
  console.log(`[docs:capture] wrote ${path}`);
}

async function captureAgentTools(page) {
  await createProject(page, "agent_tools", "Agent");
  await page.getByRole("button", { name: "Agent loop" }).click();
  const runtime = page.locator('[data-id="primary_agent"]');
  await runtime.click();

  const presetWrite = blueprintWrite(page);
  await page.getByLabel("PRESET").selectOption("deep_agent");
  await expectOk(presetWrite, "Selecting the deep_agent preset");

  const toolWrite = blueprintWrite(page);
  await page.getByRole("button", { name: /function tool/i }).first().click();
  await expectOk(toolWrite, "Adding a function tool");

  const tool = page.locator('[data-id="tool_tool"]');
  await tool.waitFor();
  const attachmentWrite = blueprintWrite(page);
  await dragCardToAttachment(page, tool, runtime);
  await expectOk(attachmentWrite, "Attaching the function tool to the runtime");
  await page.locator(".react-flow__edge--attach").waitFor({ state: "attached" });
  await runtime.click();
  await capture(page, "agent-tools.png");
}

async function captureWorkflowA2a(page) {
  await createProject(page, "workflow_a2a", "Directed workflow");
  await page.getByRole("button", { name: /Workflow · Main Workflow/ }).click();

  const firstSubagentWrite = blueprintWrite(page);
  await page.getByRole("button", { name: /subagent/i }).first().click();
  await expectOk(firstSubagentWrite, "Adding the first bound subagent");
  await page.locator('[data-id="wf_main_workflow_subagent_step"]').waitFor();

  const secondSubagentWrite = blueprintWrite(page);
  await page.getByRole("button", { name: /subagent/i }).first().click();
  await expectOk(secondSubagentWrite, "Adding the second bound subagent");
  await page.locator('[data-id="wf_main_workflow_subagent_2_step"]').waitFor();

  const source = page.locator(
    '[data-id="wf_main_workflow_subagent_step"] .node__handle--out',
  );
  const target = page.locator(
    '[data-id="wf_main_workflow_subagent_2_step"] .node__handle--in',
  );
  const dependencyWrite = blueprintWrite(page);
  await source.dragTo(target);
  await expectOk(dependencyWrite, "Connecting the bound A2A workflow steps");
  await page.locator(".react-flow__edge--flow").last().waitFor({ state: "attached" });
  await page.locator('[data-id="wf_main_workflow_subagent_2_step"]').click();
  await capture(page, "workflow-a2a.png");
}

async function captureRoutineSchedule(page) {
  await createProject(page, "routine_schedule", "Directed workflow");
  await page.getByRole("button", { name: /Workflow · Main Workflow/ }).click();

  const scheduleWrite = blueprintWrite(page);
  await page.getByRole("button", { name: /scheduled workflow/i }).click();
  await expectOk(scheduleWrite, "Adding the scheduled workflow routine");
  await page
    .getByRole("button", { name: /Routine · Scheduled workflow/ })
    .waitFor({ state: "visible" });
  await page.locator('[data-id="trigger_every_2h"]').click();
  await page.getByLabel("CRON").waitFor();
  if ((await page.getByLabel("CRON").inputValue()) !== "0 */2 * * *") {
    throw new Error("The scheduled workflow did not retain the canonical two-hour cron.");
  }
  await capture(page, "routine-schedule.png");
}

/** Hold on a moment worth watching. Screenshots stay unaffected. */
async function beat(page, times = 1) {
  await page.waitForTimeout(BEAT_MS * times);
}

/**
 * Draw a cursor that follows the real input events.
 *
 * A recording has no OS pointer, so without this the UI appears to change on its
 * own. The overlay is injected only into recording contexts — it renders from
 * genuine mouse events, so it can never show a click that did not happen.
 */
async function installCursor(context) {
  await context.addInitScript(() => {
    const install = () => {
      if (document.getElementById("__linch_cursor")) return;
      const style = document.createElement("style");
      style.textContent = `
        #__linch_cursor {
          position: fixed;
          top: 0;
          left: 0;
          width: 20px;
          height: 20px;
          margin: -10px 0 0 -10px;
          border: 2px solid #fff;
          border-radius: 50%;
          background: rgba(255, 255, 255, 0.28);
          box-shadow: 0 0 0 1.5px rgba(0, 0, 0, 0.85), 0 3px 10px rgba(0, 0, 0, 0.55);
          z-index: 2147483647;
          pointer-events: none;
          translate: -100px -100px;
          transition: scale 90ms ease-out, background 90ms ease-out;
        }
        #__linch_cursor.is-down {
          scale: 0.7;
          background: rgba(255, 255, 255, 0.95);
        }
        .__linch_ripple {
          position: fixed;
          top: 0;
          left: 0;
          width: 16px;
          height: 16px;
          margin: -8px 0 0 -8px;
          border: 2px solid #fff;
          border-radius: 50%;
          z-index: 2147483646;
          pointer-events: none;
          animation: __linch_ripple 620ms ease-out forwards;
        }
        @keyframes __linch_ripple {
          to {
            scale: 3.4;
            opacity: 0;
          }
        }
      `;
      document.head.appendChild(style);
      const dot = document.createElement("div");
      dot.id = "__linch_cursor";
      document.body.appendChild(dot);

      let x = 0;
      let y = 0;
      addEventListener(
        "mousemove",
        (event) => {
          x = event.clientX;
          y = event.clientY;
          dot.style.translate = `${x}px ${y}px`;
        },
        true,
      );
      addEventListener(
        "mousedown",
        () => {
          dot.classList.add("is-down");
          const ripple = document.createElement("div");
          ripple.className = "__linch_ripple";
          ripple.style.translate = `${x}px ${y}px`;
          document.body.appendChild(ripple);
          setTimeout(() => ripple.remove(), 640);
        },
        true,
      );
      addEventListener("mouseup", () => dot.classList.remove("is-down"), true);
    };
    if (document.body) install();
    else addEventListener("DOMContentLoaded", install);
  });
}

/** Glide the pointer onto a target so the recording shows where it went. */
async function pointTo(page, locator) {
  const box = await locator.boundingBox();
  if (!box) throw new Error("Cannot point at a target that is not visible.");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 22 });
  await page.waitForTimeout(160);
  return box;
}

/** Click the way a viewer can follow: move there, press, release. */
async function clickAt(page, locator) {
  await pointTo(page, locator);
  await page.mouse.down();
  await page.waitForTimeout(110);
  await page.mouse.up();
}

/** Point at a native select before changing it; its popup cannot be recorded. */
async function chooseAt(page, locator, value) {
  await pointTo(page, locator);
  await locator.selectOption(value);
  await page.waitForTimeout(160);
}

/** Type into a field visibly, then blur so the inspector commits the edit. */
async function typeInto(page, locator, text) {
  await clickAt(page, locator);
  await locator.fill("");
  await locator.pressSequentially(text, { delay: 45 });
  await page.waitForTimeout(200);
  await locator.blur();
}

/**
 * Record one clip of a real Studio flow into `src/assets/documentation`.
 *
 * Each clip gets its own context because Playwright writes the video only when
 * the context closes. Animation is deliberately left on — the attachment flash
 * and the running dashed edge are the whole point of these recordings.
 *
 * Args:
 *     browser: Chromium instance shared by every clip
 *     baseUrl: The `linch-studio serve` origin to drive
 *     filename: Output name, stable across runs (the bytes are not)
 *     steps: Drives the flow on a fresh page
 */
async function recordClip(browser, baseUrl, filename, steps) {
  const videoDir = await mkdtemp(join(tmpdir(), "linch-studio-clip-"));
  const context = await browser.newContext({
    baseURL: baseUrl,
    colorScheme: "dark",
    deviceScaleFactor: 1,
    locale: "en-US",
    reducedMotion: "no-preference",
    viewport: VIEWPORT,
    recordVideo: { dir: videoDir, size: VIEWPORT },
  });
  await installCursor(context);
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  try {
    await steps(page);
    await beat(page);
    const video = page.video();
    if (!video) throw new Error(`No video was recorded for ${filename}.`);
    // The clip's last frame doubles as its poster, so every video shows a real
    // end state before it is played.
    const poster = join(SCREENSHOT_DIR, filename.replace(/\.webm$/, ".png"));
    await page.screenshot({ path: poster });
    await context.close();
    const path = join(SCREENSHOT_DIR, filename);
    await video.saveAs(path);
    console.log(`[docs:capture] wrote ${path} and ${poster}`);
  } finally {
    if (context.pages().length > 0) await context.close().catch(() => {});
    await rm(videoDir, { recursive: true, force: true });
  }
}

/** Declare a Tool, then attach it explicitly and watch the Blueprint take it. */
async function clipToolAgent(page) {
  await createProjectOnCamera(page, "clip_tool_agent", "Agent");
  await clickAt(page, page.getByRole("button", { name: "Agent loop" }));
  await beat(page);

  const runtime = page.locator('[data-id="primary_agent"]');
  await clickAt(page, runtime);
  const presetWrite = blueprintWrite(page);
  await chooseAt(page, page.getByLabel("PRESET"), "deep_agent");
  await expectOk(presetWrite, "Selecting the deep_agent preset");
  await beat(page);

  const toolWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /function tool/i }).first());
  await expectOk(toolWrite, "Adding a function tool");
  // The toast and the inspector both say the tool is declared but detached.
  await page.getByTestId("tool-attachment").waitFor();
  await beat(page, 2);

  const attachWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: "Attach to primary agent" }));
  await expectOk(attachWrite, "Attaching the tool from the inspector");
  await page.locator(".canvas__connection-confirmation").waitFor();
  await beat(page, 4);
}

/** Configure the runtime rails that deliberately have no card on the canvas. */
async function clipMcpMemory(page) {
  await createProjectOnCamera(page, "clip_mcp_memory", "Agent");
  const rail = page.getByTestId("project-rail");
  await rail.waitFor();
  await beat(page);

  const settle = async (description, edit) => {
    const write = blueprintWrite(page);
    await edit();
    await expectOk(write, description);
    await beat(page);
  };

  await settle("Choosing the provider", () =>
    chooseAt(page, rail.getByLabel("PROVIDER"), "anthropic"),
  );
  await settle("Naming the API key env var", () =>
    typeInto(page, rail.getByLabel("API KEY ENV"), "ANTHROPIC_API_KEY"),
  );
  await settle("Choosing the memory backend", () =>
    chooseAt(page, rail.getByLabel("MEMORY BACKEND"), "sqlite"),
  );
  await settle("Naming the memory DSN env var", () =>
    typeInto(page, rail.getByLabel("DSN ENV"), "MEMORY_DSN"),
  );
  await settle("Enabling recall injection", () =>
    chooseAt(page, rail.getByLabel("RECALL INJECTION"), "true"),
  );
  await settle("Enabling the memory search tool", () =>
    chooseAt(page, rail.getByLabel("SEARCH TOOL"), "true"),
  );
  await settle("Declaring an HTTP MCP server", () =>
    typeInto(
      page,
      rail.getByLabel("MCP SERVERS"),
      '[{"kind":"http","id":"docs","url":"https://mcp.example.com","tokenEnv":"MCP_TOKEN"}]',
    ),
  );
  // The receipt is the point: this saved runtime wiring, not a graph edge.
  await page.getByTestId("rail-receipt").waitFor();
  await beat(page, 3);
}

/** A Subagent is a runtime member and a Skill is instructions — neither is a Tool. */
async function clipSubagentSkill(page) {
  await createProjectOnCamera(page, "clip_subagent_skill", "Agent");
  await clickAt(page, page.getByRole("button", { name: "Agent loop" }));
  await beat(page);

  const toolWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /function tool/i }).first());
  await expectOk(toolWrite, "Adding a function tool");
  await beat(page);

  const subagentWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /subagent/i }).first());
  await expectOk(subagentWrite, "Declaring a subagent runtime member");
  await beat(page, 2);

  const skillWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /skill/i }).first());
  await expectOk(skillWrite, "Declaring a skill");
  await beat(page, 2);

  const tool = page.locator('[data-id="tool_tool"]');
  const subagent = page.locator('[data-id="subagent_subagent"]');
  const toolToSubagent = blueprintWrite(page);
  await dragCardToAttachment(page, tool, subagent);
  await expectOk(toolToSubagent, "Granting the tool to the subagent");
  await beat(page, 3);

  const skill = page.locator('[data-id="skill_skill"]');
  const toolToSkill = blueprintWrite(page);
  await dragCardToAttachment(page, tool, skill);
  await expectOk(toolToSkill, "Restricting the skill's allowedTools");
  await beat(page, 3);
}

/** Two bound agent-call steps, ordered by an explicit A2A dependency. */
async function clipWorkflowMultiAgent(page) {
  await createProjectOnCamera(page, "clip_workflow_a2a", "Directed workflow");
  await clickAt(page, page.getByRole("button", { name: /Workflow · Main Workflow/ }));
  await beat(page);

  const firstWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /subagent/i }).first());
  await expectOk(firstWrite, "Adding the first bound subagent");
  await page.locator('[data-id="wf_main_workflow_subagent_step"]').waitFor();
  await beat(page, 2);

  const secondWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /subagent/i }).first());
  await expectOk(secondWrite, "Adding the second bound subagent");
  await page.locator('[data-id="wf_main_workflow_subagent_2_step"]').waitFor();
  await beat(page, 2);

  const dependencyWrite = blueprintWrite(page);
  await dragHandleToHandle(
    page,
    page.locator('[data-id="wf_main_workflow_subagent_step"] .node__handle--out'),
    page.locator('[data-id="wf_main_workflow_subagent_2_step"] .node__handle--in'),
  );
  await expectOk(dependencyWrite, "Connecting the bound A2A workflow steps");
  await page.locator(".canvas__connection-confirmation").waitFor();
  await beat(page, 4);
}

/** cron → Routine → Workflow. Studio composes it; the host runs it. */
async function clipWorkflowSchedule(page) {
  await createProjectOnCamera(page, "clip_workflow_schedule", "Directed workflow");
  await clickAt(page, page.getByRole("button", { name: /Workflow · Main Workflow/ }));
  await beat(page);

  const scheduleWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /scheduled workflow/i }));
  await expectOk(scheduleWrite, "Composing the scheduled workflow");
  await page.getByRole("button", { name: /Routine · Scheduled workflow/ }).waitFor();
  await beat(page, 2);

  await clickAt(page, page.locator('[data-id="trigger_every_2h"]'));
  await page.getByLabel("CRON").waitFor();
  await beat(page);

  const cronWrite = blueprintWrite(page);
  await typeInto(page, page.getByLabel("CRON"), "0 */6 * * *");
  await expectOk(cronWrite, "Editing the cron expression");
  await beat(page, 3);
}

/** Goal verification, an agent tick, and the cron Trigger that invokes it. */
async function clipGoalTrigger(page) {
  await createProjectOnCamera(page, "clip_goal_trigger", "Goal-verified");
  await clickAt(page, page.getByRole("button", { name: "Agent loop" }));
  await clickAt(page, page.locator('[data-id="primary_agent"]'));
  await page.getByLabel("COMPLETION").waitFor();
  await beat(page, 2);

  // Adding a routine opens its own scope; select the card to reach its fields.
  const routineWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /agent tick routine/i }));
  await expectOk(routineWrite, "Adding an agent-tick routine");
  // Routine cards keep the legacy `loop_<id>` layout key.
  const routine = page.locator('.react-flow__node[data-id^="loop_"]').first();
  await clickAt(page, routine);
  await beat(page, 2);

  const charterWrite = blueprintWrite(page);
  await typeInto(page, page.getByLabel("CHARTER"), "Keep the release checklist green.");
  await expectOk(charterWrite, "Writing the routine charter");
  await beat(page, 2);

  const triggerWrite = blueprintWrite(page);
  await clickAt(page, page.getByRole("button", { name: /cron trigger/i }));
  await expectOk(triggerWrite, "Adding a cron trigger");
  await beat(page, 2);

  const trigger = page.locator('[data-id="trigger_cron"]');
  const bindingWrite = blueprintWrite(page);
  await dragCardToAttachment(page, trigger, routine);
  await expectOk(bindingWrite, "Binding the cron trigger to the routine");
  await page.locator(".canvas__connection-confirmation").waitFor();
  await beat(page, 4);
}

const CLIPS = [
  ["capability-tool-agent.webm", clipToolAgent],
  ["capability-mcp-memory.webm", clipMcpMemory],
  ["capability-subagent-skill.webm", clipSubagentSkill],
  ["workflow-multi-agent.webm", clipWorkflowMultiAgent],
  ["workflow-schedule.webm", clipWorkflowSchedule],
  ["agent-goal-trigger.webm", clipGoalTrigger],
];

async function main() {
  const workspace = await mkdtemp(join(tmpdir(), "linch-studio-docs-"));
  const port = Number(process.env.LINCH_STUDIO_CAPTURE_PORT || (await availablePort()));
  const baseUrl = `http://127.0.0.1:${port}`;
  const output = { text: "", startError: null };
  const server = spawn(
    studioBinary(),
    ["serve", "--workspace", workspace, "--port", String(port)],
    { cwd: WEB_ROOT, stdio: ["ignore", "pipe", "pipe"] },
  );
  server.once("error", (error) => {
    output.startError = error;
  });
  for (const stream of [server.stdout, server.stderr]) {
    stream.on("data", (chunk) => {
      output.text = `${output.text}${chunk.toString()}`.slice(-12_000);
    });
  }

  let browser;
  try {
    await waitForServer(baseUrl, server, output);
    await mkdir(SCREENSHOT_DIR, { recursive: true });
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({
      baseURL: baseUrl,
      colorScheme: "dark",
      deviceScaleFactor: 1,
      locale: "en-US",
      reducedMotion: "reduce",
      viewport: VIEWPORT,
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15_000);

    await captureAgentTools(page);
    await captureWorkflowA2a(page);
    await captureRoutineSchedule(page);
    await context.close();
    console.log("[docs:capture] captured 3 documentation screenshots from real Studio UI flows.");

    for (const [filename, steps] of CLIPS) {
      await recordClip(browser, baseUrl, filename, steps);
    }
    console.log(`[docs:capture] recorded ${CLIPS.length} clips from real Studio UI flows.`);
  } finally {
    await browser?.close();
    await stopServer(server);
    await rm(workspace, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`[docs:capture] ${error.stack || error.message || error}`);
  process.exitCode = 1;
});
