// Every screen of Relay, with sample data, on a phone (light and dark) and a desktop.
// Usage: node screens.js <hub url> <hub data dir> <out dir>   (screens.sh starts a throwaway hub for it)
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { chromium } = require("playwright");

const [HUB, DATA, OUT] = process.argv.slice(2);
const ADMIN = fs.readFileSync(path.join(DATA, "admin_token"), "utf8").trim();

async function api(method, p, body, tok) {
  const r = await fetch(HUB + p, { method, headers: { "Content-Type": "application/json", ...(tok ? { Authorization: "Bearer " + tok } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const t = await r.text();
  try { return JSON.parse(t); } catch { return t; }
}
function mcp(tok, name, args) {          // an agent using a tool, as its app would
  return fetch(HUB + "/mcp", { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream",
    Authorization: "Bearer " + tok, "X-Relay-Session": "screens" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }) }).then((r) => r.json()).catch(() => null);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const db = (sql) => execFileSync("python3", ["-c", `import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2]); c.commit()`,
  path.join(DATA, "relay.db"), sql]);

async function seed() {
  const K = (await api("POST", "/owner", { handle: "kobby" }, ADMIN)).token;
  const tok = {};
  for (const [h, p] of [["claude", "claude"], ["codex", "codex"], ["muse", "muse"]])
    tok[h] = (await api("POST", "/agents", { handle: h, provider: p }, K)).token;
  const inv = await api("POST", "/invites", { handle: "ama", role: "human" }, K);
  const AMA = (await api("POST", "/invites/claim", { code: inv.code })).token;
  // a conversation with a video that the hub has already watched
  const art = await fetch(HUB + "/artifacts?name=cut-30s.mp4&mime=video/mp4", { method: "POST", headers: { Authorization: "Bearer " + tok.claude },
    body: Buffer.alloc(4000, 7) }).then((r) => r.json());
  const media = await api("POST", "/media", { artifact: art.id }, tok.claude);
  for (let i = 0; i < 40; i++) { const m = await api("GET", `/media/${media.id}`, undefined, tok.claude); if (m.status === "ready" || m.status === "failed") break; await sleep(250); }
  db(`UPDATE media SET status='ready', kind='video', digest='{"duration":30,"width":1080,"height":1920,"shots":[5,12],"transcript":[{"t":0,"text":"hi"}]}' WHERE id='${media.id}'`);
  const first = await api("POST", "/messages", { channel: "direct", text: "@claude send the 30-second cut to @codex and get it posted tonight", mentions: ["claude", "codex"] }, K);
  await api("POST", "/messages", { reply_to: first.id, text: "Sent to @codex.", media: [media.id] }, tok.claude);
  await api("POST", "/messages", { reply_to: first.id, text: "Watched it. I'll post it with “New drop. Link in bio.”" }, tok.codex);
  await api("POST", "/messages", { reply_to: first.id, text: "When should it go out?", options: ["Post now", "Tonight at 8", "Not yet"] }, tok.codex);
  // one conversation muted, one pinned: they show a mark in the list
  const noisy = await api("POST", "/messages", { channel: "direct", text: "@muse three caption ideas for the cut", mentions: ["muse"] }, K);
  await api("POST", "/messages", { reply_to: noisy.id, text: "1. New drop. 2. Out now. 3. Link in bio." }, tok.muse);
  await api("PUT", `/threads/${noisy.thread}/mark`, { muted: true }, K);
  const weekly = await api("POST", "/messages", { channel: "direct", text: "@claude every Friday: a short report on what we posted", mentions: ["claude"] }, K);
  await api("POST", "/messages", { reply_to: weekly.id, text: "Will do. First one this Friday." }, tok.claude);
  await api("PUT", `/threads/${weekly.thread}/mark`, { pinned: true }, K);
  const oldjob = await api("POST", "/messages", { channel: "direct", text: "@codex resize last week's thumbnails", mentions: ["codex"] }, K);
  await api("POST", "/messages", { reply_to: oldjob.id, text: "Done: 12 thumbnails at 1280×720." }, tok.codex);
  await api("PUT", `/threads/${oldjob.thread}/mark`, { archived: true }, K);
  // a saved key, and an agent waiting for kobby's OK to use it
  const key = await api("POST", "/keys", { name: "instagram", hosts: ["graph.facebook.com"], secret: "IGQVJ-sample-not-real-0000" }, K);
  mcp(tok.codex, "call_api", { key: "instagram", method: "POST", url: "https://graph.facebook.com/v21.0/me/media",
    json: { caption: "New drop. Link in bio.", image_url: "https://example.com/cut.jpg" }, why: "Post the 30-second cut" });
  let approval = null;
  for (let i = 0; i < 40 && !approval; i++) { const w = await api("GET", "/approvals", undefined, K); approval = Array.isArray(w) && w[0]; await sleep(250); }
  // someone asking for an invite; an app on its way in; a phone upload link
  await api("POST", "/invite-requests", { name: "Abena Owusu", contact: "abena@example.com", note: "For my shop", apps: ["claude", "chatgpt", "muse"] });
  await api("POST", "/invite-requests/friend", { name: "Kofi Boateng", contact: "kofi@example.com" }, AMA);
  const asks = await api("GET", "/invite-requests", undefined, K);
  const client = await api("POST", "/oauth/register", { client_name: "ChatGPT", redirect_uris: ["https://chatgpt.com/connector_platform_oauth_redirect"] });
  const up = await mcp(tok.claude, "request_upload", { note: "the raw clips" });
  await api("POST", "/shortcuts", { title: "Post tonight's cut", text: "@claude send the 30-second cut to @codex and get it posted tonight", mentions: ["claude", "codex"] }, K);
  const msgs = await api("GET", `/messages?thread=${first.thread}`, undefined, K);
  const card = msgs.flatMap((m) => m.media || [])[0];
  return { K, AMA, thread: first.thread, key: key.id, approval: approval && approval.id, ask: asks[0] && asks[0].id,
    client: client.client_id, upload: up && up.result && up.result.structuredContent && up.result.structuredContent.upload_url,
    mediaPage: card && card.page };
}

(async () => {
  const s = await seed();
  const auth = `/oauth/authorize?response_type=code&client_id=${s.client}&redirect_uri=${encodeURIComponent("https://chatgpt.com/connector_platform_oauth_redirect")}` +
    `&code_challenge=${"a".repeat(43)}&code_challenge_method=S256&state=x`;
  const upload = s.upload ? new URL(s.upload, HUB).pathname : null;
  // [name, path, who signs in (null: nobody), desktop too?]
  const SCREENS = [
    ["landing", "/", null, true], ["landing-demo", "/#demo", null, false], ["landing-end", "/#end", null, false],
    ["signin", "/app", null, true], ["ask-invite", "/app#invite", null, false],
    ["agents", "/app#agents", "K", true], ["agent", "/app#agents/claude", "K", true], ["wake", "/app#agents/claude/wake", "K", false],
    ["add", "/app#add", "K", true], ["add-claude", "/app#add/claude_app", "K", false], ["add-chatgpt", "/app#add/chatgpt", "K", false],
    ["add-codex", "/app#add/codex", "K", false], ["add-person", "/app#add/person", "K", false],
    ["activity", "/app#activity", "K", true], ["activity-all", "/app#activity", "K", true], ["thread", `/app#activity/${s.thread}`, "K", true],
    ["thread-menu", `/app#activity/${s.thread}`, "K", true], ["archived", "/app#activity/archived", "K", true], ["new-message", "/app#activity/new", "K", false],
    ["approve", s.approval ? `/app#approve/${s.approval}` : null, "K", true], ["invite-ask", s.ask ? `/app#ask/${s.ask}` : null, "K", false],
    ["me", "/app#me", "K", true], ["apps", "/app#me/apps", "K", false], ["passkeys", "/app#me/passkeys", "K", false], ["notify", "/app#me/notify", "K", true], ["health", "/app#me/health", "K", true], ["account", "/app#me/account", "K", true], ["account-person", "/app#me/account", "AMA", false], ["people", "/app#me/people", "K", true], ["agents-friend", "/app#agents", "AMA", false],
    ["keys", "/app#me/keys", "K", true], ["key-new", "/app#me/keys/new", "K", false], ["key", `/app#me/keys/${s.key}`, "K", false],
    ["me-person", "/app#me", "AMA", false], ["friend", "/app#me/friend", "AMA", false], ["delete", "/app#me/delete", "AMA", false],
    ["allow", auth, "K", true], ["media-page", s.mediaPage, null, true], ["upload-page", upload, null, false],
  ].filter((x) => x[1]);
  const VARIANTS = [["phone-light", { width: 390, height: 844 }, "light"], ["phone-dark", { width: 390, height: 844 }, "dark"],
    ["desktop", { width: 1280, height: 800 }, "light"]];
  const browser = await chromium.launch();
  const problems = [], a11y = [], buttons = [];
  for (const [variant, viewport, scheme] of VARIANTS) {
    for (const [name, p, who, desktop] of SCREENS) {
      if (variant === "desktop" && !desktop) continue;
      // RELAY_DPR=2: sharp enough for the landing page's phones
      const page = await browser.newPage({ viewport, colorScheme: scheme, bypassCSP: !!process.env.RELAY_AXE, deviceScaleFactor: Number(process.env.RELAY_DPR || 1) });
      page.on("pageerror", (e) => problems.push(`${variant}/${name}: ${e.message}`));
      page.on("console", (m) => { if (m.type() === "error") problems.push(`${variant}/${name}: console: ${m.text()}`); });
      await page.goto(HUB + "/app");
      await page.evaluate((k) => { localStorage.clear(); if (k) localStorage.setItem("relay_key", k); }, who ? s[who] : null);
      const [route, mark] = p.startsWith("/#") ? ["/", p.slice(2)] : [p, null];
      await page.goto(HUB + route, { waitUntil: "load" });
      if (route.includes("#")) await page.reload({ waitUntil: "load" });    // a #change alone doesn't reload the app
      if (mark === "demo") { await page.evaluate(() => document.getElementById("chat").scrollIntoView({ block: "start" })); await page.waitForTimeout(6500); }
      else if (mark === "end") { await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await page.waitForTimeout(1200); }
      else await page.waitForTimeout(route === "/" ? 3500 : 1800);
      if (name === "activity-all") {          // with few conversations there are no chips: All is what's shown
        const chip = page.locator(".fchip:visible").filter({ hasText: /^All$/ });
        if (await chip.count()) { await chip.last().click(); await page.waitForTimeout(800); }
      }
      if (name === "thread-menu") { await page.locator('[aria-label="Conversation options"]:visible').first().click(); await page.waitForTimeout(700); }
      // the page must never scroll sideways on a phone
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (wide > 1) problems.push(`${variant}/${name}: scrolls sideways by ${wide}px`);
      // nothing may stick out past the screen's edge either, even where the page clips it instead of scrolling
      const out = await page.evaluate(() => {
        const W = window.innerWidth, hits = [];
        for (const el of document.querySelectorAll("body *")) {
          const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
          if (!r.width || cs.visibility === "hidden" || cs.position === "fixed" && r.right <= W + 1) continue;
          if (r.right > W + 1 && !el.closest(".bg, .glow, [aria-hidden=true]")) hits.push(`${el.tagName.toLowerCase()}.${[...el.classList].join(".")} ends at ${Math.round(r.right)}px`);
        }
        return hits.slice(0, 3);
      });
      for (const o of out) problems.push(`${variant}/${name}: sticks out: ${o}`);
      // a slip in the code shows up as the word itself: "null", "undefined", "NaN" on the screen
      const slips = await page.evaluate(() => document.body.innerText.split("\n").map((l) => l.trim())
        .filter((l) => /^(null|undefined|NaN)$/.test(l) || /\bundefined\b|\bNaN\b/.test(l)).slice(0, 3));
      for (const o of slips) problems.push(`${variant}/${name}: shows a code slip: "${o}"`);
      // a button's words must fit inside it, with room to spare: phones use wider fonts than this browser
      // (an iPhone showed "Instant replies" spilling out of its tile), so check with letters 6% wider too
      const spill = await page.evaluate(() => {
        const st = document.createElement("style");
        st.textContent = "button, .btn, .q { letter-spacing: .06em !important; }";
        document.head.append(st);
        const hits = [];
        for (const b of document.querySelectorAll("button, .btn")) {
          const cs = getComputedStyle(b), r = b.getBoundingClientRect();
          if (!r.width || cs.visibility === "hidden" || cs.overflow === "hidden" || b.closest("[aria-hidden=true], .chat, .sky")) continue;     // the overview's count badge sits on its corner on purpose
          if (b.scrollWidth > b.clientWidth + 1) hits.push(`"${(b.textContent || "").trim().slice(0, 24)}" (${b.scrollWidth - b.clientWidth}px)`);
        }
        st.remove();
        return hits.slice(0, 4);
      });
      for (const o of spill) problems.push(`${variant}/${name}: words spill out of a button with a wider font: ${o}`);
      await page.screenshot({ path: path.join(OUT, `${variant}--${name}.png`) });
      // every button's size, so sizes can be checked against the scale (buttons.json; tools/sizes.py reads it)
      buttons.push(...(await page.evaluate(() => [...document.querySelectorAll("button, a.btn, .btn, [role=button], input[type=submit]")].map((b) => {
        const r = b.getBoundingClientRect(), cs = getComputedStyle(b);
        if (!r.width || !r.height || cs.visibility === "hidden" || b.closest("[aria-hidden=true]")) return null;
        return { cls: (typeof b.className === "string" ? b.className : "").trim(), text: (b.textContent || b.getAttribute("aria-label") || "").trim().slice(0, 30),
          h: Math.round(r.height), w: Math.round(r.width), font: cs.fontSize, weight: cs.fontWeight, radius: cs.borderTopLeftRadius, bg: cs.backgroundColor };
      }).filter(Boolean))).map((b) => ({ ...b, screen: `${variant}/${name}` })));
      // RELAY_AXE=<path to axe.min.js>: also check each screen for accessibility problems (axe-core)
      if (process.env.RELAY_AXE) {          // phone and desktop: the sidebar is only on a desktop
        await page.addScriptTag({ path: process.env.RELAY_AXE });
        const v = await page.evaluate(async () => (await axe.run(document, { resultTypes: ["violations"] })).violations
          .map((x) => ({ id: x.id, impact: x.impact, help: x.help, nodes: x.nodes.slice(0, 3).map((n) => n.target.join(" ")) })));
        for (const x of v) a11y.push(`${variant}/${name}: [${x.impact}] ${x.id}: ${x.help} — ${x.nodes.join(" | ")}`);
      }
      await page.close();
    }
  }
  await browser.close();
  fs.writeFileSync(path.join(OUT, "problems.txt"), problems.join("\n") + "\n");
  fs.writeFileSync(path.join(OUT, "buttons.json"), JSON.stringify(buttons));
  if (process.env.RELAY_AXE) { fs.writeFileSync(path.join(OUT, "a11y.txt"), a11y.join("\n") + "\n"); console.log(`${a11y.length} accessibility finding(s), in a11y.txt`); }
  console.log(problems.length ? `${problems.length} problem(s):\n` + problems.join("\n") : "No script errors, nothing scrolls sideways.");
  process.exit(0);
})();
