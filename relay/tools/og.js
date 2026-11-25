// Renders og.html to relay/web/og.png, the picture a shared Relay link shows.
const path = require("path");
const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1200, height: 630 } });
  await p.goto("file://" + path.join(__dirname, "og.html"));
  await p.waitForTimeout(400);
  await p.screenshot({ path: path.join(__dirname, "..", "web", "og.png") });
  await b.close();
})();
