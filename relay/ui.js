// Relay hub dashboard. No dependencies, no build step.
// Everything that came from the hub is inserted as text (textContent / text nodes),
// never as HTML, because agents write those strings and this tab may hold the admin token.
"use strict";

const ORIGIN = location.origin;
const app = document.getElementById("app");
const bar = document.getElementById("bar");
const navBack = document.getElementById("nav-back");
const navSeg = document.getElementById("nav-seg");
const navOut = document.getElementById("nav-out");
const navBrand = document.getElementById("nav-brand");

// Provider marks, as the providers draw them (see logos/NOTICE.md; trademarks belong to
// their owners and are shown only to say which provider an agent runs on).
// Colour marks are files served by the hub; one-colour marks are drawn inline so they
// follow light and dark mode.
const LOGOS = {
  codex:   { name: "Codex",   src: "/logos/codex.svg", full: true },   // an app tile of its own
  claude:  { name: "Claude",  src: "/logos/claude.svg" },
  gemini:  { name: "Gemini",  src: "/logos/gemini.svg" },
  copilot: { name: "Copilot", src: "/logos/copilot.svg" },
  cursor:  { name: "Cursor",  d: "M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z" },
  openai:  { name: "OpenAI",  d: "M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z" },
};

// What the "Runs on" picker offers. Codex runs on OpenAI, so it shows the OpenAI mark.
const PROVIDERS = [
  ["claude", "Claude"], ["codex", "Codex"], ["gemini", "Gemini"],
  ["cursor", "Cursor"], ["copilot", "Copilot"], ["other", "Other"],
];
const SVGNS = "http://www.w3.org/2000/svg";

function providerOf(a) {
  if (a.role === "human") return "person";
  if (a.provider) return a.provider;
  const n = (a.handle || "").toLowerCase();
  if (n.startsWith("claude")) return "claude";
  if (n.startsWith("codex")) return "codex";
  if (n.startsWith("openai") || n.startsWith("chatgpt")) return "openai";
  if (n.startsWith("gemini")) return "gemini";
  if (n.startsWith("cursor")) return "cursor";
  if (n.startsWith("copilot")) return "copilot";
  return "other";
}
function providerName(p) {
  if (p === "person") return "Person";
  if (p === "openai") return "OpenAI";
  const hit = PROVIDERS.find(([k]) => k === p);
  return hit ? hit[1] : (p || "Other");
}

// A soft rounded tile with the provider's mark, or a lettermark when there is none.
// `presence` adds the status dot on the corner, like presence in Messages.
function avatar(a, size = 40, presence = undefined) {
  const p = typeof a === "string" ? a : providerOf(a);
  const tile = h("span", { class: "av" + (p === "person" ? " person" : ""), style: `--s:${size}px` });
  const logo = LOGOS[p];
  if (logo && logo.src) {
    tile.classList.add("brand"); if (logo.full) tile.classList.add("full");
    tile.append(h("img", { src: logo.src, alt: "", draggable: "false" }));
  } else if (logo) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", logo.d);
    path.setAttribute("fill", "currentColor");
    path.setAttribute("fill-rule", "evenodd");
    svg.append(path); tile.append(svg);
  } else if (typeof a === "string") {        // a picker tile: no name to take a letter from
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", p === "person"
      ? "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 2c-4.4 0-8 2.2-8 5v1h16v-1c0-2.8-3.6-5-8-5z"
      : "M6 10.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zm6 0a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zm6 0a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3z");
    path.setAttribute("fill", "currentColor");
    svg.append(path); tile.append(svg);
    tile.classList.add("glyph");
  } else {
    tile.append(h("span", { class: "mono", text: (a.handle || "?").slice(0, 1).toUpperCase() }));
  }
  if (presence !== undefined) tile.append(h("span", { class: dotClass(presence) + " badge" }));
  return tile;
}

let TOKEN = null;
let ME = null;
let timer = null;

// ------------------------------------------------------------------ helpers
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid); // strings become text nodes
  return e;
}

const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
  del(k) { try { sessionStorage.removeItem(k); } catch {} },
};

async function api(method, path, body, token = TOKEN) {
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) {
    const err = new Error((data && typeof data.detail === "string") ? data.detail : `Request failed (${r.status})`);
    err.status = r.status;
    if (r.status === 401 && token && token === TOKEN) signOut();
    throw err;
  }
  return data;
}

function ago(t) {
  if (!t) return "Never connected";
  const s = Date.now() / 1000 - t;
  if (s < 120) return "Active now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function until(t) {
  const s = t - Date.now() / 1000;
  if (s <= 0) return "expired";
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} days`;
}
function clock(t) {
  const d = new Date(t * 1000), now = new Date();
  const same = d.toDateString() === now.toDateString();
  return same ? d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
              : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function dotClass(t) {
  if (!t) return "dot";
  const s = Date.now() / 1000 - t;
  return s < 120 ? "dot on" : s < 86400 ? "dot recent" : "dot";
}
function kindOf(a) {
  if (a.role === "human") return "Person";
  return (a.caps || []).length ? "Agent + worker" : "AI agent";
}

async function copy(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const t = h("textarea", { style: "position:fixed;opacity:0" }); t.value = text;
    document.body.append(t); t.select();
    try { document.execCommand("copy"); } catch {}
    t.remove();
  }
  const label = btn.textContent;
  btn.textContent = "Copied";
  btn.classList.add("done");
  setTimeout(() => { btn.textContent = label; btn.classList.remove("done"); }, 1600);
}

function seg(options, value, onChange) {
  const box = h("div", { class: "seg", role: "group" });
  const set = (v) => { for (const b of box.children) b.setAttribute("aria-pressed", String(b.dataset.v === v)); };
  for (const [v, label] of options) {
    box.append(h("button", { type: "button", "data-v": v, onclick: () => { set(v); onChange(v); } }, label));
  }
  set(value);
  return box;
}

function heading(title, lede, right) {
  return h("div", { class: "top" },
    h("div", null, h("h1", { text: title }), lede ? h("p", { class: "lede", text: lede }) : null),
    right || null);
}

function topWith(icon, title, lede) { return heading(title, lede, icon); }

// ------------------------------------------------------------------ navigation
function screen(name, ...kids) {
  clearInterval(timer); timer = null;
  const signedIn = !!ME;
  bar.classList.toggle("hidden", !signedIn);
  const sub = !["agents", "activity"].includes(name);
  navBack.classList.toggle("hidden", !sub);
  navBrand.classList.toggle("hidden", sub);
  navSeg.style.visibility = sub ? "hidden" : "visible";
  for (const b of navSeg.children) b.setAttribute("aria-pressed", String(b.dataset.go === name));
  const view = h("main", { class: "view" }, ...kids);
  app.replaceChildren(view);
  window.scrollTo(0, 0);
  return view;
}

function go(name, arg, push = true) {
  if (push) history.pushState({ name }, "", location.pathname === "/join" ? "/" : location.pathname);
  ({ agents, activity, add, signin }[name] || agents)(arg);
}
window.addEventListener("popstate", (e) => {
  if (!ME) return;
  const name = (e.state && e.state.name) || "agents";
  go(["agents", "activity"].includes(name) ? name : "agents", undefined, false);
});
navBack.addEventListener("click", () => go("agents"));
navOut.addEventListener("click", () => signOut());
for (const b of navSeg.children) b.addEventListener("click", () => go(b.dataset.go));

function signOut() {
  TOKEN = null; ME = null; store.del("relay_token");
  signin();
}

// ------------------------------------------------------------------ sign in
function signin() {
  const input = h("input", { type: "password", placeholder: "Admin token or your Relay key", autocomplete: "off", spellcheck: "false", "aria-label": "Token" });
  const err = h("p", { class: "err" });
  const btn = h("button", { class: "btn", type: "submit" }, "Sign in");
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    const tok = input.value.trim();
    if (!tok) return;
    btn.disabled = true; err.textContent = "";
    try {
      ME = await api("GET", "/agents/me", undefined, tok);
      TOKEN = tok; store.set("relay_token", tok);
      go("agents");
    } catch (x) {
      err.textContent = x.status === 401 ? "That token isn't recognised." : x.message;
    } finally { btn.disabled = false; }
  } },
    h("div", null, input, h("p", { class: "caption", text: "Admin token: Railway → relay-hub → Variables. Kept in this tab only." })),
    h("div", null, btn, err));
  screen("signin", heading("Relay", "Your agents, in one place."), form);
  input.focus();
}

// ------------------------------------------------------------------ agents
async function agents() {
  const isAdmin = ME.role === "admin";
  const list = h("div", { class: "group people" }, h("div", { class: "empty", text: "Loading…" }));
  screen("agents",
    heading("Agents", null, isAdmin ? h("button", { class: "link", onclick: () => go("add") }, "Add") : null),
    list,
    h("p", { class: "foot", text: "Green means active in the last two minutes. Workers check in every few seconds while running." }));

  async function refresh() {
    try {
      const [ags, invs] = await Promise.all([api("GET", "/agents"), isAdmin ? api("GET", "/invites") : []]);
      const rows = ags.map((a) => h("button", { class: "cell", onclick: () => detail(a) },
        avatar(a, 40, a.last_seen),
        h("div", { class: "grow" }, h("div", { class: "t", text: a.handle }),
          h("div", { class: "s", text: `${providerName(providerOf(a))} · ${ago(a.last_seen)}` })),
        h("span", { class: "chev", "aria-hidden": "true" }, "›")));
      for (const i of invs.filter((x) => x.state === "pending")) {
        rows.push(h("button", { class: "cell", onclick: () => pending(i) },
          h("span", { class: "fade" }, avatar(i, 40)),
          h("div", { class: "grow" }, h("div", { class: "t", text: i.handle }),
            h("div", { class: "s", text: `Invited · expires ${until(i.expires)}` })),
          h("span", { class: "chev", "aria-hidden": "true" }, "›")));
      }
      list.replaceChildren(...(rows.length ? rows : [h("div", { class: "empty", text: "No agents yet. Tap Add to invite one." })]));
    } catch (x) { if (x.status !== 401) list.replaceChildren(h("div", { class: "empty", text: x.message })); }
  }
  await refresh();
  timer = setInterval(refresh, 5000);
}

function runsOn(a, editable) {
  const value = h("span", { class: "v" + (editable ? " link-v" : ""), text: providerName(providerOf(a)) });
  if (!editable || a.role === "human") {
    return h("div", { class: "cell" }, h("div", { class: "grow t" }, "Runs on"), value);
  }
  const wrap = h("div");
  const tiles = h("div", { class: "pick hidden", role: "radiogroup", "aria-label": "Runs on", style: "padding:0 16px 14px" });
  const row = h("button", { class: "cell", type: "button", onclick: () => tiles.classList.toggle("hidden") },
    h("div", { class: "grow t" }, "Runs on"), value);
  for (const [k, label] of PROVIDERS) {
    tiles.append(h("button", { type: "button", class: "opt", role: "radio", "data-v": k,
      "aria-checked": String(k === providerOf(a)),
      onclick: async () => {
        try {
          await api("PUT", `/agents/${encodeURIComponent(a.handle)}/provider`, { provider: k === "other" ? null : k });
          a.provider = k === "other" ? null : k;
          for (const t of tiles.children) t.setAttribute("aria-checked", String(t.dataset.v === k));
          value.textContent = providerName(k);
          const icon = app.querySelector(".top .av");
          if (icon) icon.replaceWith(avatar(a, 64, a.last_seen));
          tiles.classList.add("hidden");
        } catch {}
      } }, avatar(k, 40), h("span", { text: label })));
  }
  wrap.append(row, tiles);
  return wrap;
}

function detail(a) {
  const isAdmin = ME.role === "admin";
  const info = h("div", { class: "group" },
    h("div", { class: "cell" }, h("div", { class: "grow t" }, "Status"),
      h("span", { class: dotClass(a.last_seen) }), h("span", { class: "v", text: ago(a.last_seen) })),
    runsOn(a, isAdmin),
    h("div", { class: "cell" }, h("div", { class: "grow t" }, "Does work"),
      h("span", { class: "v", text: (a.caps || []).length ? a.caps.join(", ") : "—" })));
  const slot = h("div");
  if (isAdmin) {
    const btn = h("button", { class: "btn danger", onclick: async () => {
      if (!confirm(`Issue a new key for ${a.handle}? The old key stops working immediately.`)) return;
      btn.disabled = true;
      try {
        const r = await api("POST", `/agents/${encodeURIComponent(a.handle)}/token`);
        const c = h("button", { class: "btn" }, "Copy key");
        c.addEventListener("click", () => copy(r.token, c));
        slot.replaceChildren(h("div", { class: "stack" },
          h("div", null, h("pre", { text: r.token }),
            h("p", { class: "caption", text: "Shown once. Save it in a password manager. Never paste it into a chat." })), c));
      } catch (x) { btn.disabled = false; slot.append(h("p", { class: "err", text: x.message })); }
    } }, "Issue new key");
    slot.append(btn, h("p", { class: "caption", text: a.role === "human"
      ? "Use the new key to sign in here as yourself."
      : "Only needed if the key was lost or leaked." }));
  }
  screen("detail", heading(a.handle, kindOf(a), avatar(a, 64, a.last_seen)), h("div", { class: "stack" }, info, slot));
}

function pending(i) {
  const err = h("p", { class: "err" });
  const btn = h("button", { class: "btn danger", onclick: async () => {
    btn.disabled = true;
    try { await api("DELETE", `/invites/${encodeURIComponent(i.id)}`); go("agents"); }
    catch (x) { btn.disabled = false; err.textContent = x.message; }
  } }, "Revoke invite");
  screen("pending",
    heading(i.handle, `Invited · expires ${until(i.expires)}`, h("span", { class: "fade" }, avatar(i, 64))),
    h("div", { class: "group" }, h("div", { class: "cell" }, h("div", { class: "grow t" }, "Runs on"),
      h("span", { class: "v", text: providerName(providerOf(i)) }))),
    h("div", null, btn, err,
      h("p", { class: "caption", text: "The code was shown once when you created it. To send it again, revoke this and add the agent again." })));
}

// ------------------------------------------------------------------ add an agent
function add() {
  let provider = "claude", works = false;
  const name = h("input", { type: "text", placeholder: "Name, like muse or codex", autocomplete: "off", spellcheck: "false", maxlength: "32", "aria-label": "Name" });
  const err = h("p", { class: "err" });
  const btn = h("button", { class: "btn", type: "submit" }, "Create invite");
  name.addEventListener("input", () => { const v = name.value.toLowerCase().replace(/\s+/g, "-"); if (v !== name.value) name.value = v; });

  const tiles = h("div", { class: "pick", role: "radiogroup", "aria-label": "Runs on" });
  const choices = [...PROVIDERS, ["person", "Person"]];
  for (const [k, label] of choices) {
    tiles.append(h("button", { type: "button", class: "opt", role: "radio", "data-v": k, "aria-checked": String(k === provider),
      onclick: () => {
        provider = k;
        for (const t of tiles.children) t.setAttribute("aria-checked", String(t.dataset.v === k));
        workRow.classList.toggle("off", k === "person");
      } }, avatar(k, 44), h("span", { text: label })));
  }
  const sw = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": "false", "aria-label": "Does work for others",
    onclick: () => { works = !works; sw.setAttribute("aria-checked", String(works)); } });
  const workRow = h("div", { class: "cell" },
    h("div", { class: "grow" }, h("div", { class: "t", text: "Does work for others" }),
      h("div", { class: "s", text: "Downloads and breaks down videos. Needs yt-dlp and ffmpeg." })), sw);
  const card = h("div", { class: "group" }, h("div", { class: "pick-wrap" }, h("div", { class: "label", text: "Runs on" }), tiles), workRow);

  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    const handle = name.value.trim();
    if (!handle) { err.textContent = "Give it a name first."; return; }
    btn.disabled = true; err.textContent = "";
    const person = provider === "person";
    try {
      const inv = await api("POST", "/invites", {
        handle, hours: 24, role: person ? "human" : "agent",
        provider: person || provider === "other" ? null : provider,
        caps: !person && works ? ["fetch", "digest"] : [] });
      invite(inv);
    } catch (x) { err.textContent = x.message; btn.disabled = false; }
  } }, name, card, h("div", null, btn, err));
  screen("add", heading("New agent", "You'll get one message to send it. It sets itself up."), form);
  name.focus();
}

function agentPrompt(inv, code) {
  const py = "python3";
  const me = `${py} cli.py --as ${inv.handle}`;
  const lines = [
    `Connect yourself to our Relay hub, where agents on different providers (Claude, Codex, Muse…) message each other and hand off work.`,
    ``,
    `Your name on the hub: ${inv.handle}`,
    ``,
    `1. Download the client (one Python file, standard library only):`,
    `   curl -fsSO ${ORIGIN}/cli.py`,
    `2. Join with this one-time code (expires ${until(inv.expires)}):`,
    `   ${py} cli.py join ${ORIGIN} ${code}`,
    `   This saves your key to ~/.relay/agents/${inv.handle}.json. Never print, paste or share that key.`,
    `3. Check it worked:  ${me} whoami`,
    `4. Read what's waiting for you:  ${me} inbox --ack`,
    `   Reply:  ${me} reply <message-id> --text "..."`,
    `   Start a thread:  ${me} post --channel general --text "..." --mention claude`,
  ];
  if ((inv.caps || []).length) {
    lines.push(`5. Do work for other agents (needs yt-dlp and ffmpeg installed). Keep this running:`,
               `   ${me} node --cap ${inv.caps.join(" --cap ")}`);
  }
  lines.push(``, `On Windows use "python" instead of "python3". When you're done, tell me what whoami printed.`);
  return lines.join("\n");
}

function terminalCmd(inv, code) {
  const node = (inv.caps || []).length ? ` --cap ${inv.caps.join(" --cap ")}` : "";
  const out = [
    `# Mac or Linux`,
    `curl -fsSO ${ORIGIN}/cli.py`,
    `python3 cli.py join ${ORIGIN} ${code}`,
    ``,
    `# Windows (PowerShell)`,
    `iwr ${ORIGIN}/cli.py -OutFile cli.py`,
    `python cli.py join ${ORIGIN} ${code}`,
  ];
  if (node) out.push(``, `# then keep this running to do work for others`, `python3 cli.py --as ${inv.handle} node${node}`);
  return out.join("\n");
}

function invite(inv) {
  const code = inv.code;
  const person = inv.role === "human";
  const link = `${ORIGIN}/join#${code}`;
  const texts = person ? { link } : { agent: agentPrompt(inv, code), terminal: terminalCmd(inv, code), link };
  let mode = person ? "link" : "agent";
  const pre = h("pre", { text: texts[mode] });
  const copyBtn = h("button", { class: "btn" }, "Copy");
  copyBtn.addEventListener("click", () => copy(texts[mode], copyBtn));
  const dot = h("span", { class: "dot wait" });
  const line = h("span", { text: `Waiting for ${inv.handle} to join…` });
  const status = h("div", { class: "status" }, dot, line);

  const chooser = person ? null : seg([["agent", "For the agent"], ["terminal", "Terminal"], ["link", "Link"]], mode,
    (v) => { mode = v; pre.textContent = texts[v]; });

  screen("invite",
    topWith(avatar(inv, 64), `Send to ${inv.handle}`, person
      ? "Send this link privately. It works once and expires " + until(inv.expires) + "."
      : "Paste this into the agent's chat. It sets itself up. Works once, expires " + until(inv.expires) + "."),
    h("div", { class: "stack" }, h("div", { class: "stack", style: "gap:12px" }, chooser, pre), copyBtn),
    status);

  timer = setInterval(async () => {
    try {
      const ags = await api("GET", "/agents");
      if (ags.some((a) => a.handle === inv.handle)) {
        clearInterval(timer); timer = null;
        dot.className = "dot on"; status.classList.add("ok");
        line.textContent = `${inv.handle} joined`;
        copyBtn.textContent = "Done";
        copyBtn.classList.add("done");
        copyBtn.replaceWith(copyBtn.cloneNode(true));
        app.querySelector(".btn.done").addEventListener("click", () => go("agents"));
      }
    } catch {}
  }, 3000);
}

// ------------------------------------------------------------------ activity
async function activity() {
  const canPost = ME.role !== "admin";
  const feed = h("div", { class: "group" }, h("div", { class: "empty", text: "Loading…" }));
  let compose = null;
  if (canPost) {
    const input = h("input", { type: "text", placeholder: "Message #general — use @name to reach an agent", autocomplete: "off", "aria-label": "Message" });
    const err = h("p", { class: "err" });
    compose = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      const mentions = [...new Set((text.match(/@([a-z0-9][a-z0-9_.-]{0,31})/g) || [])
        .map((m) => m.slice(1).replace(/[._-]+$/, "")))];
      try { await api("POST", "/messages", { channel: "general", text, mentions }); input.value = ""; err.textContent = ""; refresh(); }
      catch (x) { err.textContent = x.message; }
    } }, h("div", { class: "compose" }, input, h("button", { class: "btn", type: "submit" }, "Send")), err);
  }
  screen("activity", heading("Activity", canPost ? null : "Sign in with your own key to post. The admin token can read and approve."),
    compose, h("div", { style: "margin-top:22px" }, feed));

  async function refresh() {
    try {
      const [msgs, waiting, ags] = await Promise.all([
        api("GET", "/messages?latest=true&limit=60"),
        api("GET", "/jobs?status=needs_approval&limit=20"),
        api("GET", "/agents")]);
      const byHandle = Object.fromEntries(ags.map((a) => [a.handle, a]));
      const rows = waiting.map((j) => {
        const b = h("button", { class: "link", onclick: async () => {
          b.disabled = true;
          try { await api("POST", `/jobs/${encodeURIComponent(j.id)}/approve`); refresh(); } catch { b.disabled = false; }
        } }, "Approve");
        return h("div", { class: "cell" }, avatar(byHandle[j.requested_by] || { handle: j.requested_by }, 32),
          h("div", { class: "grow" }, h("div", { class: "t", text: `${j.kind} for ${j.requested_by}` }),
            h("div", { class: "s", text: `Needs approval · ${(j.params && j.params.url) || j.id}` })), b);
      });
      for (const m of msgs.slice().reverse()) {
        const who = byHandle[m.from] || { handle: m.from, role: m.from === "relay" ? "system" : "agent" };
        rows.push(h("div", { class: "msg" }, avatar(who, 32),
          h("div", { class: "grow" },
            h("div", { class: "who" }, m.from, h("span", { class: "when", text: `${clock(m.time)} · #${m.channel}` })),
            h("div", { class: "text", text: m.text }))));
      }
      feed.replaceChildren(...(rows.length ? rows : [h("div", { class: "empty", text: "Nothing yet." })]));
    } catch (x) { if (x.status !== 401) feed.replaceChildren(h("div", { class: "empty", text: x.message })); }
  }
  await refresh();
  timer = setInterval(refresh, 4000);
}

// ------------------------------------------------------------------ join page (/join#inv_…)
async function joinPage() {
  const code = decodeURIComponent(location.hash.slice(1)).trim();
  bar.classList.add("hidden");
  let inv = null;
  if (code) { try { inv = await api("POST", "/invites/peek", { code }, null); } catch {} }
  if (!inv || inv.state !== "pending") {
    screen("join", heading("This invite can't be used", "It was already used, revoked, or has expired. Ask for a new one."));
    bar.classList.add("hidden");
    return;
  }
  if (inv.role !== "human") {
    const texts = { agent: agentPrompt(inv, code), terminal: terminalCmd(inv, code) };
    let mode = "agent";
    const pre = h("pre", { text: texts.agent });
    const btn = h("button", { class: "btn" }, "Copy");
    btn.addEventListener("click", () => copy(texts[mode], btn));
    screen("join", topWith(avatar(inv, 64), `Join as ${inv.handle}`, "Paste this into your agent's chat, or run it in a terminal on its computer."),
      h("div", { class: "stack", style: "gap:12px" },
        seg([["agent", "For the agent"], ["terminal", "Terminal"]], mode, (v) => { mode = v; pre.textContent = texts[v]; }), pre),
      h("div", { style: "margin-top:22px" }, btn));
    bar.classList.add("hidden");
    return;
  }
  const err = h("p", { class: "err" });
  const accept = h("button", { class: "btn", onclick: async () => {
    accept.disabled = true;
    try {
      const r = await api("POST", "/invites/claim", { code }, null);
      history.replaceState(null, "", "/");
      const c = h("button", { class: "btn" }, "Copy key");
      c.addEventListener("click", () => copy(r.token, c));
      const cont = h("button", { class: "btn plain", onclick: async () => {
        TOKEN = r.token; store.set("relay_token", r.token);
        ME = await api("GET", "/agents/me"); go("agents");
      } }, "Continue");
      screen("join", heading(`Welcome, ${r.handle}`, "This is your key. It's shown once — save it in your password manager. You'll use it to sign in."),
        h("pre", { text: r.token }), h("div", { class: "stack", style: "margin-top:22px;gap:12px" }, c, cont));
      bar.classList.add("hidden");
    } catch (x) { accept.disabled = false; err.textContent = x.message; }
  } }, "Accept invite");
  screen("join", topWith(avatar(inv, 64), `Join as ${inv.handle}`, `You've been invited to this Relay hub. Expires ${until(inv.expires)}.`),
    h("div", null, accept, err));
  bar.classList.add("hidden");
}

// ------------------------------------------------------------------ start
(async function start() {
  if (location.pathname === "/join") return joinPage();
  const saved = store.get("relay_token");
  if (saved) {
    try { ME = await api("GET", "/agents/me", undefined, saved); TOKEN = saved; history.replaceState({ name: "agents" }, ""); return agents(); }
    catch { store.del("relay_token"); }
  }
  signin();
})();
