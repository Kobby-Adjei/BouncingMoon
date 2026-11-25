// Activity and conversations the way a real hub fills up: dozens of conversations over days, long messages,
// long links, raw data from websites, code, many agents in one thread. Photographs them at phone, tablet and
// desktop widths and reports anything that scrolls sideways, inside the page as well as the page itself.
// Usage: node heavy.js <hub url> <hub data dir> <out dir>   (screens.sh --heavy runs it on a throwaway hub)
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { chromium } = require("playwright");

const [HUB, DATA, OUT] = process.argv.slice(2);
const ADMIN = fs.readFileSync(path.join(DATA, "admin_token"), "utf8").trim();
const api = (m, p, b, t) => fetch(HUB + p, { method: m, headers: { "Content-Type": "application/json", ...(t ? { Authorization: "Bearer " + t } : {}) },
  body: b === undefined ? undefined : JSON.stringify(b) }).then((r) => r.json());
const db = (sql, ...args) => execFileSync("python3", ["-c", "import sqlite3,sys,json; c=sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2], json.loads(sys.argv[3])); c.commit()",
  path.join(DATA, "relay.db"), sql, JSON.stringify(args)]);

const LONG_URL = "https://graph.facebook.com/v21.0/17841400008460056/media?image_url=" + encodeURIComponent("https://cdn.example.com/uploads/2026/11/24/launch-cut-final-final-v7-1080x1920-with-captions-and-music.jpg") + "&caption=" + encodeURIComponent("New drop. Link in bio. #launch #ai #relay");
const JSON_BLOB = JSON.stringify({ id: "17895695668004550", status_code: "FINISHED", permalink: "https://www.instagram.com/p/C9xYzAbCdEf/", media: Array.from({ length: 8 }, (_, i) => ({ id: "1790000000" + i, type: "IMAGE", url: "https://scontent.cdninstagram.com/v/t51.29350-15/" + "x".repeat(40) + i + ".jpg" })) });
const CODE = "```js\nexport async function postToInstagram({ caption, imageUrl, accessToken = process.env.IG_ACCESS_TOKEN_THAT_IS_VERY_LONG }) {\n  const res = await fetch(`https://graph.facebook.com/v21.0/me/media?image_url=${encodeURIComponent(imageUrl)}&caption=${encodeURIComponent(caption)}`, { method: 'POST' });\n  return res.json();\n}\n```";
const ESSAY = "Here's the plan for launch week. ".repeat(4) + "Monday: teaser. Tuesday: the 30-second cut. Wednesday: behind the scenes. ".repeat(6) + "Let me know what to change.";
const MANY = "@claude @codex @muse @chatgptguy @grokguy @claude-code-laptop @gemini-cli-office-mac please all review the launch checklist before 6pm";

(async () => {
  const K = (await api("POST", "/owner", { handle: "kobby" }, ADMIN)).token;
  const tok = {};
  const agents = [["claude", "claude"], ["codex", "codex"], ["muse", "muse"], ["chatgptguy", "openai"], ["grokguy", "grok"],
    ["claude-code-laptop", "claude"], ["gemini-cli-office-mac", "gemini"]];
  for (const [h, p] of agents) tok[h] = (await api("POST", "/agents", { handle: h, provider: p }, K)).token;
  const H = agents.map((a) => a[0]);
  const lines = ["Can you check the deck?", "Done: 12 slides, fixed the typos.", LONG_URL, JSON_BLOB, CODE, ESSAY, MANY,
    "ok", "👍🏾 sounds good", "Uploading the raw clips now", "What's the status on the captions?", "Merged. Deploying in 5."];
  const threads = [];
  for (let i = 0; i < 36; i++) {
    const a = H[i % H.length], b = H[(i * 3 + 1) % H.length] === a ? H[(i + 2) % H.length] : H[(i * 3 + 1) % H.length];
    const starter = i % 4 === 0 ? K : tok[a];
    const first = await api("POST", "/messages", { channel: "direct", text: lines[i % lines.length], mentions: i % 9 === 0 ? H.filter((x) => x !== a) : [b] }, starter);
    threads.push(first.thread);
    for (let j = 0; j < (i % 5) + 1; j++)
      await api("POST", "/messages", { reply_to: first.id, text: lines[(i + j + 3) % lines.length] }, j % 2 ? tok[a] : tok[b]);
  }
  // the hub's own notes, as a real hub has them
  for (const t of threads.slice(0, 6)) await api("POST", "/messages", { reply_to: t, text: "🔑 200 · " + JSON_BLOB }, tok.codex);
  // spread them over two weeks, as they really happen
  const now = Date.now() / 1000;
  threads.forEach((t, i) => db("UPDATE messages SET time = time - ? WHERE thread=?", i < 6 ? i * 600 : i < 14 ? 86400 + i * 3600 : 86400 * (2 + i % 12), t));
  // and something waiting for kobby
  await api("POST", "/invite-requests", { name: "Abena Owusu-Mensah", contact: "abena.owusu.mensah@a-very-long-company-domain.example.com", note: "Claude and ChatGPT for my shop" });

  const browser = await chromium.launch();
  const problems = [];
  // the narrow sidebar (960 to 1023px) and a dark desktop too
  const sizes = [["phone", 390, 844], ["tablet", 768, 1024], ["small-laptop", 980, 700], ["laptop", 1024, 700], ["desktop", 1440, 900],
    ["desktop-dark", 1440, 900, "dark"]];
  const pick = [threads[0], threads[3], threads[6], threads[27]];
  for (const [name, w, hgt, scheme] of sizes) {
    const page = await browser.newPage({ viewport: { width: w, height: hgt }, colorScheme: scheme || "light" });
    page.on("pageerror", (e) => problems.push(`${name}: ${e.message}`));
    await page.goto(HUB + "/app");
    await page.evaluate((k) => localStorage.setItem("relay_key", k), K);
    for (const [label, route] of [["activity", "activity"], ...pick.map((t, i) => [`thread${i + 1}`, `activity/${t}`])]) {
      await page.goto(HUB + "/app#" + route); await page.reload(); await page.waitForTimeout(2200);   // a #change alone doesn't reload
      const found = await page.evaluate(() => {
        const W = window.innerWidth, out = [];
        const d = document.documentElement;
        if (d.scrollWidth > d.clientWidth + 1) out.push(`the page scrolls sideways by ${d.scrollWidth - d.clientWidth}px`);
        for (const el of document.querySelectorAll("body *")) {
          const cs = getComputedStyle(el);
          if (cs.display === "none" || cs.visibility === "hidden") continue;
          const name = `${el.tagName.toLowerCase()}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : ""}`;
          if (/(auto|scroll)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 1 && !el.matches("pre.block.oneline"))
            out.push(`${name} scrolls sideways inside (${el.scrollWidth - el.clientWidth}px)`);
          const r = el.getBoundingClientRect();
          if (r.width && r.right > W + 1 && !el.closest("[aria-hidden=true]")) out.push(`${name} sticks out to ${Math.round(r.right)}px`);
        }
        return [...new Set(out)].slice(0, 6);
      });
      for (const f of found) problems.push(`${name}/${label}: ${f}`);
      await page.screenshot({ path: path.join(OUT, `heavy-${name}-${label}.png`), fullPage: name === "phone" });
    }
    // the @ picker with every agent in it
    await page.goto(HUB + "/app#activity/" + pick[0]); await page.reload(); await page.waitForTimeout(1500);
    const box = await page.$(".composer textarea, .composer input");
    if (box) { await box.click(); await box.type("@"); await page.waitForTimeout(500);
      const wide = await page.evaluate(() => { const p = document.querySelector(".atpick"); return p ? p.scrollWidth - p.clientWidth : 0; });
      if (wide > 1) problems.push(`${name}/at-picker: scrolls sideways by ${wide}px`);
      await page.screenshot({ path: path.join(OUT, `heavy-${name}-atpick.png`) }); }
    await page.close();
  }
  await browser.close();
  fs.writeFileSync(path.join(OUT, "heavy-problems.txt"), problems.join("\n") + "\n");
  console.log(problems.length ? `${problems.length} problem(s):\n` + problems.join("\n") : "Nothing scrolls sideways at any size.");
})();
