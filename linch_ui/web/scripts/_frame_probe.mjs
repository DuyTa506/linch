import { chromium } from "@playwright/test";

const VIDEO_PATH = process.argv[2];
const TIME_S = Number(process.argv[3] || "10");
const OUT_PATH = process.argv[4] || "/tmp/frame.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.setContent(
  `<video id="v" src="file://${VIDEO_PATH}" style="width:1440px;height:900px"></video>`,
);
const video = page.locator("#v");
const duration = await page.evaluate(
  ({ t }) =>
    new Promise((resolve, reject) => {
      const el = document.getElementById("v");
      el.onloadedmetadata = () => {
        el.currentTime = Math.min(t, el.duration - 0.1);
      };
      el.onseeked = () => resolve(el.duration);
      el.onerror = () => reject(new Error("video load error"));
    }),
  { t: TIME_S },
);
await video.screenshot({ path: OUT_PATH });
await browser.close();
console.log("duration", duration, "wrote", OUT_PATH);
