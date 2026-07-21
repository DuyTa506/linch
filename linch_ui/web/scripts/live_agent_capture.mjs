/**
 * Record a real conversational-authoring session against a live provider.
 *
 * Unlike `docs:capture` (which boots its own offline serve), this script drives
 * an ALREADY RUNNING `linch-studio serve` that has real `LINCH_STUDIO_*`
 * credentials, walking the full ask → options → plan → approve → proposal →
 * accept flow in a recorded browser with a synthetic cursor. It is the
 * env-gated manual smoke for AI authoring; CI never runs it.
 *
 *   LIVE_BASE_URL=http://127.0.0.1:8901 LIVE_OUT_DIR=/tmp/demo \
 *     node scripts/live_agent_capture.mjs
 */

import { mkdir, rm } from "node:fs/promises";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { chromium } from "@playwright/test";

const BASE_URL = process.env.LIVE_BASE_URL || "http://127.0.0.1:8901";
const OUT_DIR = resolve(process.env.LIVE_OUT_DIR || "live-capture");
const PROJECT_ID = process.env.LIVE_PROJECT_ID || "nightly_review";
const REQUEST =
  process.env.LIVE_REQUEST ||
  "Build a nightly code-review pipeline: fetch the latest git diff, review it, and write a summary report.";
const FREEFORM_ANSWER =
  process.env.LIVE_FREEFORM || "Keep it simple and pick the most sensible default.";
const VIEWPORT = { width: 1440, height: 900 };
const BEAT_MS = 900;
/** A model turn with reasoning can take minutes; the build turn the longest. */
const TURN_TIMEOUT = 420_000;
const BUILD_TIMEOUT = 600_000;

const beat = (page, times = 1) => page.waitForTimeout(BEAT_MS * times);

/** Same synthetic cursor as docs:capture — drawn from genuine input events. */
async function installCursor(context) {
  await context.addInitScript(() => {
    const install = () => {
      if (document.getElementById("__linch_cursor")) return;
      const style = document.createElement("style");
      style.textContent = `
        #__linch_cursor { position: fixed; top: 0; left: 0; width: 20px; height: 20px;
          margin: -10px 0 0 -10px; border: 2px solid #fff; border-radius: 50%;
          background: rgba(255,255,255,0.28);
          box-shadow: 0 0 0 1.5px rgba(0,0,0,0.85), 0 3px 10px rgba(0,0,0,0.55);
          z-index: 2147483647; pointer-events: none; translate: -100px -100px;
          transition: scale 90ms ease-out, background 90ms ease-out; }
        #__linch_cursor.is-down { scale: 0.7; background: rgba(255,255,255,0.95); }
        .__linch_ripple { position: fixed; top: 0; left: 0; width: 16px; height: 16px;
          margin: -8px 0 0 -8px; border: 2px solid #fff; border-radius: 50%;
          z-index: 2147483646; pointer-events: none;
          animation: __linch_ripple 620ms ease-out forwards; }
        @keyframes __linch_ripple { to { scale: 3.4; opacity: 0; } }
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

async function pointTo(page, locator) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("Cannot point at a target that is not visible.");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 22 });
  await page.waitForTimeout(160);
}

async function clickAt(page, locator) {
  await pointTo(page, locator);
  await page.mouse.down();
  await page.waitForTimeout(110);
  await page.mouse.up();
}

async function typeInto(page, locator, text) {
  await clickAt(page, locator);
  await locator.pressSequentially(text, { delay: 30 });
  await page.waitForTimeout(200);
}

/** Expand the newest dimmed thinking drop-down so the trace is on camera. */
async function revealThinking(page) {
  const summaries = page.locator("details.msg__thinking summary");
  const count = await summaries.count();
  if (count === 0) return;
  await clickAt(page, summaries.nth(count - 1));
  await beat(page, 2);
}

/** Hold the camera on the live token-by-token reasoning while a turn runs. */
async function watchStream(page) {
  const stream = page.locator(".msg--stream");
  try {
    await stream.waitFor({ timeout: 20_000 });
    await stream.scrollIntoViewIfNeeded();
    await beat(page, 5);
  } catch {
    // The turn may have landed before any reasoning streamed; nothing to film.
  }
}

/** Hold the camera on the live compact tool-call line(s) while a turn runs. */
async function watchToolCalls(page) {
  const calls = page.locator(".msg__tool-calls").last();
  try {
    await calls.waitFor({ timeout: 8_000 });
    await calls.scrollIntoViewIfNeeded();
    await beat(page, 2);
  } catch {
    // This turn made no knowledge-tool call; nothing to film.
  }
}

/** Expand the newest settled tool-call row: compact line, then the full detail. */
async function revealToolCalls(page) {
  const rows = page.locator("details.msg__tool-call");
  const count = await rows.count();
  if (count === 0) return;
  const summary = rows.nth(count - 1).locator("summary");
  await pointTo(page, summary);
  await beat(page);
  await clickAt(page, summary);
  await beat(page, 2);
}

/** Answer every question: options for all but the last, free input for the last. */
async function answerQuestions(page) {
  const form = page.locator(".msg__form");
  const questions = form.locator(".msg__question");
  const total = await questions.count();
  for (let index = 0; index < total; index += 1) {
    const options = questions.nth(index).locator(".msg__option");
    const optionCount = await options.count();
    if (index === total - 1 && optionCount > 1) {
      // The last button is always the UI-owned free-form option.
      await clickAt(page, options.nth(optionCount - 1));
      await typeInto(page, questions.nth(index).locator(".msg__other-input"), FREEFORM_ANSWER);
    } else {
      await clickAt(page, options.first());
    }
  }
  await clickAt(page, form.getByRole("button", { name: "send answers" }));
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const videoDir = await mkdtemp(join(tmpdir(), "linch-live-"));
  const browser = await chromium.launch();
  const context = await browser.newContext({
    baseURL: BASE_URL,
    colorScheme: "dark",
    deviceScaleFactor: 1,
    locale: "en-US",
    viewport: VIEWPORT,
    recordVideo: { dir: videoDir, size: VIEWPORT },
  });
  await installCursor(context);
  const page = await context.newPage();
  page.setDefaultTimeout(TURN_TIMEOUT);

  try {
    // A fresh project, created on camera.
    await page.goto("/");
    await beat(page);
    await clickAt(page, page.getByRole("button", { name: "+ new blueprint" }));
    await typeInto(page, page.getByRole("dialog").getByLabel("PROJECT ID"), PROJECT_ID);
    await clickAt(page, page.getByRole("button", { name: "create", exact: true }));
    await page.locator(".topbar", { hasText: `[${PROJECT_ID}]` }).waitFor();
    await beat(page);

    // Ask the agent.
    await clickAt(page, page.getByRole("button", { name: /ai-assist/ }));
    await beat(page);
    const input = page.locator(".drawer__foot input");
    await typeInto(page, input, REQUEST);
    await clickAt(page, page.locator(".drawer__foot").getByRole("button"));
    await watchStream(page);
    await watchToolCalls(page);

    // Questions (possibly more than one round), then the plan.
    for (let round = 0; round < 3; round += 1) {
      await page.locator(".msg__form, .msg__plan").first().waitFor();
      if ((await page.locator(".msg__plan").count()) > 0) break;
      await revealThinking(page);
      await revealToolCalls(page);
      await answerQuestions(page);
      await watchStream(page);
      await watchToolCalls(page);
    }
    await page.locator(".msg__plan").waitFor();
    await revealThinking(page);
    await revealToolCalls(page);
    await beat(page);

    // The approval gate: nothing was built yet; approving flips to build stage.
    // A failed build turn rolls the synthetic approval back and re-arms the
    // gate, so simply approving again is the retry.
    page.setDefaultTimeout(BUILD_TIMEOUT);
    const accept = page.getByRole("button", { name: "accept proposal" });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await clickAt(page, page.getByRole("button", { name: "build this plan" }));
      await watchStream(page);
      await watchToolCalls(page);
      await accept.or(page.getByRole("button", { name: "build this plan" })).first().waitFor();
      if ((await accept.count()) > 0) break;
      await beat(page);
    }
    await accept.waitFor();
    await revealThinking(page);
    await revealToolCalls(page);
    await pointTo(page, page.getByRole("button", { name: "accept proposal" }));
    await beat(page, 2);

    // Accept the proposal, then show the blueprint that came out of it.
    await clickAt(page, page.getByRole("button", { name: "accept proposal" }));
    await page.locator(".msg--agent", { hasText: "accepted into the blueprint" }).waitFor();
    await beat(page, 2);
    await clickAt(page, page.locator(".drawer__head button[aria-label='close']"));
    await beat(page, 3);

    // The interesting result of a hard request is the generated DAG, not the
    // project map: jump to the first Workflow scope tab and let it settle.
    const workflowTab = page.locator(".scopebar__item", { hasText: "Workflow ·" }).first();
    if ((await workflowTab.count()) > 0) {
      await clickAt(page, workflowTab);
      await beat(page, 2);
      await clickAt(page, page.getByRole("button", { name: "fit", exact: true }));
      await beat(page, 3);
    }

    await page.screenshot({ path: join(OUT_DIR, "live_agent_demo.png") });
    const video = page.video();
    await context.close();
    await video.saveAs(join(OUT_DIR, "live_agent_demo.webm"));
    console.log(`[live:capture] wrote ${join(OUT_DIR, "live_agent_demo.webm")}`);
  } finally {
    if (context.pages().length > 0) await context.close().catch(() => {});
    await browser.close();
    await rm(videoDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`[live:capture] ${error.stack || error.message || error}`);
  process.exitCode = 1;
});
