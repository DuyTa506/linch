import { expect, test } from "@playwright/test";

test.describe("in-app documentation", () => {
  test("opens from the homepage and loads screenshots captured from real flows", async ({ page }) => {
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Build your first agent system in 10 minutes" }),
    ).toBeVisible();

    await page.getByRole("button", { name: /open getting started/ }).click();
    await expect(page).toHaveURL(/\/docs$/);
    await expect(page.getByTestId("documentation-page")).toBeVisible();
    await expect(
      page.getByRole("heading", {
        name: "From an agent to a scheduled multi-agent workflow",
      }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Create an agent and attach a tool" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Build a multi-agent A2A workflow" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Schedule the workflow outside its graph" })).toBeVisible();

    const images = page.locator(".docs__figure img");
    await expect(images).toHaveCount(3);
    for (let index = 0; index < 3; index += 1) {
      await expect
        .poll(() => images.nth(index).evaluate((image: HTMLImageElement) => image.naturalWidth))
        .toBeGreaterThan(1000);
    }

    await page.goBack();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator(".home")).toBeVisible();
  });

  test("serves a playable clip for every recipe", async ({ page }) => {
    await page.goto("/docs");
    const videos = page.locator(".docs__video");
    await expect(videos).toHaveCount(6);

    for (let index = 0; index < 6; index += 1) {
      const video = videos.nth(index);
      const source = await video.locator("source").getAttribute("src");
      expect(source).toMatch(/\.webm$/);
      // The bundled asset must really be there and really be video.
      const response = await page.request.get(source!);
      expect(response.status()).toBe(200);
      expect(response.headers()["content-type"]).toContain("video/webm");

      // Decoding the metadata proves the browser can actually play the bytes,
      // not merely fetch them.
      const width = await video.evaluate(
        (element: HTMLVideoElement) =>
          new Promise<number>((resolve, reject) => {
            if (element.readyState >= 1) return resolve(element.videoWidth);
            element.addEventListener("loadedmetadata", () => resolve(element.videoWidth), {
              once: true,
            });
            element.addEventListener("error", () => reject(new Error("decode failed")), {
              once: true,
            });
            element.load();
          }),
      );
      expect(width).toBeGreaterThan(0);
      expect(await video.evaluate((element: HTMLVideoElement) => element.autoplay)).toBe(false);
    }
  });

  test("teaches the runtime rails and keeps clips inside a narrow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/docs");

    await expect(
      page.getByRole("heading", { name: "Wire the provider, memory, and MCP rails" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: /Add skills and subagents/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: /Verify the goal and let a trigger/ }),
    ).toBeVisible();

    const firstVideo = (await page.locator(".docs__video").first().boundingBox())!;
    expect(firstVideo.width).toBeLessThanOrEqual(390);
    const pageWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(pageWidth).toBeLessThanOrEqual(390);
  });

  test("supports a direct docs URL and a narrow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/docs");

    await expect(page.getByTestId("documentation-page")).toBeVisible();
    await expect(page.locator(".docs__nav")).toBeVisible();
    const firstAxis = (await page.locator(".docs__axis").nth(0).boundingBox())!;
    const secondAxis = (await page.locator(".docs__axis").nth(1).boundingBox())!;
    expect(secondAxis.y).toBeGreaterThan(firstAxis.y + firstAxis.height);
    const pageWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(pageWidth).toBeLessThanOrEqual(390);
  });
});
