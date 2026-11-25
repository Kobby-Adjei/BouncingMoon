// The first five minutes, walked the way a brand-new person walks them, with a photo at every step.
// Owner: fresh hub -> make it yours -> passkey -> add Claude (Connect, Allow) -> first message -> a reply ->
// invite a friend. Friend: invite link -> passkey -> connect their own app -> say hello.
// Usage: node firstrun.js <hub url> <hub data dir> <out dir> <phone|desktop>   (firstrun.sh runs both)
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { chromium } = require("playwright");

const [HUB, DATA, OUT, KIND] = process.argv.slice(2);
const ADMIN = fs.readFileSync(path.join(DATA, "admin_token"), "utf8").trim();
const VIEW = KIND === "desktop" ? { width: 1280, height: 800 } : { width: 390, height: 844 };
const notes = [];
let n = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const b64u = (b) => b.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function shot(page, name, wait = 900) {
  await page.waitForTimeout(wait);
  const file = `${KIND}--${String(++n).padStart(2, "0")}-${name}.png`;
  await page.screenshot({ path: path.join(OUT, file), fullPage: KIND !== "desktop" });
  const text = (await page.evaluate(() => document.body.innerText)).replace(/\s+/g, " ").trim().slice(0, 400);
  notes.push(`${file}\n  ${text}`);
}
async function passkeys(page) {          // a phone's Face ID, simulated: every passkey prompt just works
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", { options: { protocol: "ctap2", transport: "internal", hasResidentKey: true,
    hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
}
async function mcp(tok, name, args) {
  const r = await fetch(HUB + "/mcp", { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream",
    Authorization: "Bearer " + tok, "X-Relay-Session": "firstrun" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }) });
  return r.json();
}
// an app connecting the way claude.ai does: register, open the Allow page, come back with a code, swap it for a token
async function connectApp(ctx, appName, redirect, label) {
  const reg = await fetch(HUB + "/oauth/register", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: appName, redirect_uris: [redirect] }) }).then((r) => r.json());
  const verifier = b64u(crypto.randomBytes(32)), challenge = b64u(crypto.createHash("sha256").update(verifier).digest());
  const page = await ctx.newPage();
  await passkeys(page);
  let code = null;
  await page.route(redirect.split("?")[0] + "**", (route) => { code = new URL(route.request().url()).searchParams.get("code"); route.fulfill({ body: "back in the app" }); });
  await page.goto(`${HUB}/oauth/authorize?response_type=code&client_id=${reg.client_id}&redirect_uri=${encodeURIComponent(redirect)}` +
    `&code_challenge=${challenge}&code_challenge_method=S256&state=s`);
  await shot(page, `${label}-allow`, 1500);
  await page.click("#allow");
  for (let i = 0; i < 40 && !code; i++) await sleep(250);
  await page.close();
  const tok = await fetch(HUB + "/oauth/token", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: redirect, client_id: reg.client_id, code_verifier: verifier }) }).then((r) => r.json());
  return tok.access_token;
}
const click = async (page, sel) => { await page.locator(sel).first().click(); };

(async () => {
  const browser = await chromium.launch();
  // ---------------------------------------------------------------- the owner
  const own = await browser.newContext({ viewport: VIEW, colorScheme: "light" });
  const p = await own.newPage();
  await passkeys(p);
  p.on("pageerror", (e) => notes.push(`!! script error: ${e.message}`));
  await p.goto(HUB + "/app");
  await shot(p, "signin", 1500);
  await click(p, "button.way:has-text('Use a key')");
  await p.fill("input[type=password]", ADMIN);
  await click(p, "button[type=submit]");
  await shot(p, "make-it-yours");
  await p.fill("input.field:visible", "kobby");
  await click(p, "button[type=submit]");
  await shot(p, "owner-passkey");
  await click(p, "button:has-text('Add a passkey')");
  await shot(p, "owner-home", 2500);
  // add Claude
  await click(p, KIND === "desktop" ? ".railadd" : "button:has-text('Add')");
  await shot(p, "add-pick");
  await click(p, ".opt:has-text('Claude')");
  await shot(p, "add-claude");
  const claude = await connectApp(own, "Claude", "https://claude.ai/api/mcp/auth_callback", "owner-claude");
  await shot(p, "claude-connected", 3500);
  // the next step is right there: say hello
  await click(p, "button:has-text('Say hello')");
  await shot(p, "thread-blank");
  const box = p.locator(".composer textarea");
  await box.fill("Hi Claude, can you hear me through Relay?");
  await box.press("Enter");
  if (KIND !== "desktop") await click(p, ".composer .send");
  await shot(p, "sent-and-waiting", 2000);
  const got = await mcp(claude, "check_replies", {});
  const msg = got.result && got.result.structuredContent && (got.result.structuredContent.messages || [])[0];
  if (msg) await mcp(claude, "reply", { message_id: msg.id, text: "Loud and clear. I'm on Relay now: ask me to pass anything to your other agents." });
  else notes.push("!! Claude found nothing in check_replies: " + JSON.stringify(got).slice(0, 300));
  await shot(p, "reply", 5000);
  await p.goto(HUB + "/app#me/apps"); await p.reload();
  await shot(p, "connected-apps", 1500);
  await p.goto(HUB + "/app"); await p.reload();
  await shot(p, "bare-app-again", 2500);
  // invite a friend
  await click(p, KIND === "desktop" ? ".railadd" : "button:has-text('Add')").catch(async () => { await p.goto(HUB + "/app#add"); await p.reload(); });
  await click(p, ".opt:has-text('Person')");
  await shot(p, "invite-person");
  await p.fill("input.field:visible", "ama");
  await click(p, "button[type=submit]");
  await shot(p, "invite-link");
  let link = await p.evaluate(() => (document.body.innerText.match(/https?:\/\/\S+\/join#inv_[\w-]+/) || [])[0]);
  if (!link) {
    notes.push("!! no join link visible on the page: made one by API");
    const tok = await p.evaluate(() => localStorage.getItem("relay_key") || sessionStorage.getItem("relay_key"));
    const inv = await fetch(HUB + "/invites", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + tok },
      body: JSON.stringify({ handle: "ama2", role: "human", hours: 72 }) }).then((r) => r.json());
    link = `${HUB}/join#${inv.code}`;
  }
  // ---------------------------------------------------------------- the friend, on their own phone
  const fr = await browser.newContext({ viewport: VIEW, colorScheme: "dark" });
  const q = await fr.newPage();
  await passkeys(q);
  q.on("pageerror", (e) => notes.push(`!! script error (friend): ${e.message}`));
  await q.goto(link.replace(/^https?:\/\/[^/]+/, HUB));
  await shot(q, "friend-invited", 1500);
  await click(q, "button:has-text('Join')");
  await shot(q, "friend-passkey");
  await click(q, "button:has-text('Add a passkey')");
  await shot(q, "friend-first-screen", 2500);
  await click(q, ".opt:has-text('ChatGPT')");
  await shot(q, "friend-chatgpt-steps");
  const gpt = await connectApp(fr, "ChatGPT", "https://chatgpt.com/connector_platform_oauth_redirect", "friend-chatgpt");
  await shot(q, "friend-connected", 3500);
  await click(q, "button:has-text('Say hello')");
  await shot(q, "friend-thread-blank");
  const fb = q.locator(".composer textarea");
  await fb.fill("Hello from Ama's ChatGPT side");
  await fb.press("Enter");
  if (KIND !== "desktop") await click(q, ".composer .send");
  await shot(q, "friend-sent-and-waiting", 2000);
  await q.goto(HUB + "/app#agents"); await q.reload();
  await shot(q, "friend-agents", 1500);
  await q.goto(HUB + "/app#me/apps"); await q.reload();
  await shot(q, "friend-apps", 1500);
  await q.goto(HUB + "/app"); await q.reload();
  await shot(q, "friend-bare-app", 2500);
  notes.push(`(friend's ChatGPT token: ${gpt ? "issued" : "MISSING"})`);
  await browser.close();
  fs.writeFileSync(path.join(OUT, `${KIND}-notes.txt`), notes.join("\n") + "\n");
  console.log(`${n} photos, notes in ${KIND}-notes.txt`);
})().catch((e) => { console.error(e); fs.writeFileSync(path.join(OUT, `${KIND}-notes.txt`), notes.join("\n") + "\n!! stopped: " + e.message + "\n"); process.exit(1); });
