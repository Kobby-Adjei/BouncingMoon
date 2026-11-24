// Landing page: a short demo of how Relay feels, played once when it comes into view.
// You ask Claude; Claude hands Muse the video, already watched; Muse asks to post; you tap Approve.
// It's a demo: it never talks to the hub, shows nothing about anyone's agents, and posts nothing.
(function () {
"use strict";
const log = document.getElementById("log");
if (!log) return;
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
const NS = "http://www.w3.org/2000/svg";
const label = document.getElementById("runlabel"), chip = document.getElementById("chip");

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function svg(viewBox, parts, attrs) {
  const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", viewBox); s.setAttribute("aria-hidden", "true");
  for (const [k, v] of Object.entries(attrs || {})) s.setAttribute(k, v);
  for (const [tag, a] of parts) { const p = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(a)) p.setAttribute(k, v); s.append(p); }
  return s;
}
function withMentions(parent, text) {          // "@claude" in bold, the rest as plain text
  for (const part of text.split(/(@[a-z0-9._-]+)/)) {
    if (!part) continue;
    parent.append(part.startsWith("@") ? el("span", "at", part) : document.createTextNode(part));
  }
}
const tickSvg = () => svg("0 0 30 30", [["path", { d: "M8 15.5l4.6 4.6L22.5 10", stroke: "#fff", "stroke-width": "3.2", "stroke-linecap": "round", "stroke-linejoin": "round" }]], { fill: "none" });
function avatar(who) {
  const av = el("span", "av");
  const img = el("img"); img.src = `/logos/${who}.svg`; img.alt = ""; av.append(img);
  return av;
}

// ---------------------------------------------------------------- the exchange (seconds from the start)
const SCRIPT = [
  { t: 0.3, from: "you", text: "@claude send the 30-second cut to @muse and get it posted tonight.", file: "cut-30s.mp4 · 18 MB" },
  { t: 1.5, from: "claude", text: "Sent to Muse.", card: true },
  { t: 3.1, from: "muse", text: "Watched it. I'll post it with “New drop. Link in bio.”" },
  { t: 4.4, from: "relay", ask: true },
];

const rows = SCRIPT.map((m) => {
  const row = el("li", m.from === "you" ? "row me" : "row");
  const b = el("div", "b");
  const step = { m, row, b };
  if (m.from === "you") {
    withMentions(b, m.text);
    const f = el("div", "file");
    f.append(svg("0 0 24 24", [["rect", { x: 3, y: 5, width: 18, height: 14, rx: 3 }], ["path", { d: "M10 9.5v5l4.5-2.5z", fill: "currentColor", stroke: "none" }]],
      { fill: "none", stroke: "currentColor", "stroke-width": "1.8" }), document.createTextNode(m.file));
    b.append(f);
    row.append(b);
    return step;
  }
  const body = el("div", "body"), who = el("div", "who"), st = el("span", "st");
  step.st = st;
  if (m.ask) {                                  // Relay asks you: the two halves wait for your tap
    b.className = "b ask";
    const orb = el("span", "morb");
    orb.append(el("span", "mh l"), el("span", "mh r"), el("span", "kseal"));
    orb.firstChild.append(avatarImg("muse")); orb.children[1].append(el("i", "kh")); orb.lastChild.append(tickSvg());
    const words = el("div", "askw");
    words.append(el("b", null, "Muse wants to post"), el("span", null, "with your Instagram key · it never sees it"));
    const go = el("button", "go", "Approve"); go.type = "button";
    const ok = el("span", "okay", "Posted. (A demo: nothing was sent.)");
    go.addEventListener("click", () => approve(step));
    b.append(orb, words, go, ok);
    step.orb = orb;
    who.append(el("b", null, "Relay"), st);
    body.append(who, b);
    const av = el("span", "av relay"); av.append(el("i", "mk"));
    row.append(av, body);
    return step;
  }
  withMentions(b, m.text);
  if (m.card) {                                  // the video arrives already watched
    const card = el("div", "card");
    const poster = el("span", "poster");
    poster.append(svg("0 0 24 24", [["path", { d: "M8 5.5v13l11-6.5z", fill: "#fff" }]]));
    const meta = el("div", "meta");
    meta.append(el("b", null, "cut-30s.mp4"), el("span", null, "0:30 · 1080×1920"), el("span", "seen", "Already watched: 3 shots · captions in · loud enough"));
    card.append(poster, meta);
    b.append(card);
  }
  who.append(el("b", null, m.from), st);
  body.append(who, b);
  row.append(avatar(m.from), body);
  return step;
});
function avatarImg(who) { const i = el("img"); i.src = `/logos/${who}.svg`; i.alt = ""; return i; }
rows.forEach((s) => log.append(s.row));

// ---------------------------------------------------------------- playing it
let timers = [], playing = false;
const later = (ms, fn) => timers.push(setTimeout(fn, ms));

function approve(s) {
  s.b.classList.add("approved"); s.orb.classList.add("sealed");
  s.st.textContent = "approved"; s.st.className = "st done";
}
function finalState(s) {
  s.row.className = s.m.from === "you" ? "row me" : "row"; s.b.classList.remove("hold");
  if (s.m.ask) { s.st.textContent = "needs you"; s.st.className = "st ask"; }
}
function reset() {
  timers.forEach(clearTimeout); timers = [];
  for (const s of rows) {
    s.row.className = s.m.from === "you" ? "row me later" : "row later";
    s.b.classList.add("hold"); s.b.classList.remove("show", "approved");
    if (s.st) { s.st.textContent = ""; s.st.className = "st"; }
    if (s.orb) s.orb.classList.remove("sealed");
  }
}
function play() {
  reset(); playing = true;
  label.textContent = "A demo · nothing is sent";
  for (const s of rows) {
    later(s.m.t * 1000, () => { s.row.classList.replace("later", "show"); s.b.classList.replace("hold", "show"); finalState(s); });
  }
  later(SCRIPT[SCRIPT.length - 1].t * 1000 + 700, () => { playing = false; label.textContent = "Your turn: tap Approve"; });
}
function still() {                                     // reduced motion: the whole exchange, at rest
  reset();
  for (const s of rows) finalState(s);
  label.textContent = "A demo · nothing is sent";
}

chip.addEventListener("click", () => {
  document.getElementById("chat").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  if (reduce) still(); else if (!playing) play();
});
if (reduce) { still(); return; }
reset();
// start when the demo is in view
if (!("IntersectionObserver" in window)) { play(); return; }
const seen = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { seen.disconnect(); play(); } }, { threshold: 0.35 });
seen.observe(log);
})();
