// Specials: four ready-made crews, each told by a short scene. A scene plays once when it scrolls
// into view and again on a tap; it never loops, and with reduced motion it simply shows how it ends.
// Everything is drawn with DOM calls into #specials-grid; this file never talks to the hub.
(function () {
"use strict";
const root = document.getElementById("specials-grid");
if (!root) return;
const NS = "http://www.w3.org/2000/svg";
const motion = "IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches;
const LOGO = { claude: "/logos/claude.svg", codex: "/logos/codex.svg", gemini: "/logos/gemini.svg", muse: "/logos/muse.svg" };
let uid = 0;

// ------------------------------------------------------------------ tiny builders
function s(tag, attrs, ...kids) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) n.setAttribute(k, v);
  n.append(...kids.filter((k) => k != null));
  return n;
}
function h(tag, cls, ...kids) {
  const n = document.createElement(tag); if (cls) n.className = cls; n.append(...kids); return n;
}
const g = (...kids) => s("g", {}, ...kids);
const at = (x, y, ...kids) => s("g", { transform: `translate(${x} ${y})` }, ...kids);
// mark a node to animate: the CSS holds the keyframes, the base style is always the end state
function a(node, kind, d, t, vars) {
  node.classList.add("sp-a", "sp-" + kind);
  node.style.setProperty("--d", d + "s");
  if (t) node.style.setProperty("--t", t + "s");
  for (const [k, v] of Object.entries(vars || {})) node.style.setProperty("--" + k, v);
  return node;
}
const bar = (x, y, w, hh = 7, cls = "sp-ln") => s("rect", { class: cls, x, y, width: w, height: hh, rx: hh / 2 });
const card = (w, hh, r = 12) => s("rect", { class: "sp-card", width: w, height: hh, rx: r });
const wire = (d) => s("path", { class: "sp-wire", d, pathLength: 1 });
const text = (x, y, cls, str, anchor) => s("text", { class: cls, x, y, "text-anchor": anchor || null }, str);

function tile(x, y, n, logo, d) {
  const r = n * .28;
  let body;
  if (logo === "codex") {                       // the Codex mark is a whole app tile: show it as is
    const id = "sp-clip-" + (++uid);
    body = g(s("defs", {}, s("clipPath", { id }, s("rect", { width: n, height: n, rx: r }))),
             s("image", { href: LOGO.codex, width: n, height: n, "clip-path": `url(#${id})` }),
             s("rect", { class: "sp-hair", width: n, height: n, rx: r }));
  } else {
    body = g(s("rect", { class: "sp-tile", width: n, height: n, rx: r }),
             s("image", { href: LOGO[logo], x: n * .22, y: n * .22, width: n * .56, height: n * .56 }));
  }
  return at(x, y, a(body, "pop", d, .45));
}
function check(cx, cy, r, d) {
  return g(a(s("circle", { class: "sp-ok", cx, cy, r }), "pop", d, .3),
           a(s("path", { class: "sp-okk", pathLength: 1,
                         d: `M${cx - r * .42} ${cy + r * .02}l${r * .3} ${r * .3} ${r * .56} -${r * .6}` }), "draw", d + .12, .3));
}
// the human's gate: an outlined pill that you press; it fills with the special's colour and ticks
function gate(cx, cy, w, label, d, press) {
  const hh = 38, x = -w / 2, y = -hh / 2, lw = label.length * 10.5;
  const shift = 9, tx = shift - lw / 2 - 19;
  const pill = g(
    s("rect", { class: "sp-gate-o", x, y, width: w, height: hh, rx: hh / 2 }),
    a(text(0, 6.5, "sp-gate-t", label, "middle"), "out", press + .02, .18),
    a(s("rect", { class: "sp-gate-f", x, y, width: w, height: hh, rx: hh / 2 }), "in", press + .04, .22),
    a(text(shift, 6.5, "sp-gate-ft", label, "middle"), "in", press + .04, .22),
    a(s("path", { class: "sp-gate-k", d: `M${tx} 0.5l4 4 7.5 -8`, pathLength: 1 }), "draw", press + .14, .3));
  return at(cx, cy, a(g(a(g(pill), "press", press, .32)), "pop", d, .4));
}
function ping(cx, cy, w, hh, r, d) {       // one ring that says "this one", then fades
  return at(cx, cy, a(s("rect", { class: "sp-ping", x: -w / 2, y: -hh / 2, width: w, height: hh, rx: r }), "ping", d, .9));
}
function cursor(x, y, d, press) {           // the tip rests at (x, y)
  return at(x, y, a(g(a(g(s("path", { class: "sp-cur", d: "M0 0l15 8.6-6.4 1.6L5.2 17z" })), "press", press, .32)),
                    "glide", d, .5, { x: "54px", y: "40px" }));
}
function scene(w, hh, ...kids) {
  return s("svg", { viewBox: `0 0 ${w} ${hh}`, "aria-hidden": "true", focusable: "false" }, ...kids);
}

// ------------------------------------------------------------------ Relay Clips
// scout brief → writer script (facts ticked) → editor cut → you press GO → the clip lands on a phone
function clips() {
  const lines = [64, 56, 64, 36, 50].map((w, i) => a(bar(12, 14 + i * 12, w), "grow", 1.45 + i * .08, .3));
  const holes = [];
  for (let i = 0; i < 8; i++) holes.push(s("rect", { class: "sp-hole", x: 5 + i * 12.6, y: 4, width: 6, height: 5, rx: 1.5 }),
                                         s("rect", { class: "sp-hole", x: 5 + i * 12.6, y: 55, width: 6, height: 5, rx: 1.5 }));
  const frames = [6, 37, 68].map((x, i) => a(s("rect", { class: "sp-frame", x, y: 14, width: 26, height: 36, rx: 3,
                                                       }), "pop", 2.72 + i * .1, .3));
  const screen = "sp-clip-" + (++uid);
  return scene(560, 260,
    a(wire("M82 50H148"), "draw", .98, .35),
    a(wire("M208 50H274"), "draw", 2.22, .35),
    a(wire("M334 50H386a12 12 0 0 1 12 12V132"), "draw", 3.0, .4),
    a(wire("M430 151H452"), "draw", 4.08, .2),
    tile(22, 20, 60, "gemini", 0), tile(148, 20, 60, "claude", .08), tile(274, 20, 60, "muse", .16),
    at(8, 104, a(g(card(88, 84),
                   bar(12, 14, 50), bar(12, 26, 34, 6, "sp-ln2"),
                   a(s("path", { class: "sp-spark", d: "M12 70l14-10 12 5 16-18 18-9", pathLength: 1 }), "draw", .6, .55),
                   a(s("circle", { class: "sp-dot", cx: 72, cy: 38, r: 4.5 }), "pop", 1.1, .3)), "rise", .38)),
    at(134, 104, a(g(card(88, 104), ...lines, check(70, 86, 9.5, 1.95)), "rise", 1.28)),
    at(254, 116, a(g(s("rect", { class: "sp-film", width: 100, height: 64, rx: 6 }), ...holes, ...frames), "rise", 2.55)),
    at(452, 52, a(g(
      s("rect", { class: "sp-phone", width: 92, height: 196, rx: 18 }),
      s("defs", {}, s("clipPath", { id: screen }, s("rect", { x: 6, y: 6, width: 80, height: 184, rx: 13 }))),
      s("g", { "clip-path": `url(#${screen})` },
        s("rect", { class: "sp-screen", x: 6, y: 6, width: 80, height: 184 }),
        a(g(s("rect", { class: "sp-fill", x: 6, y: 6, width: 80, height: 184 }),
            a(s("path", { class: "sp-on-fill", d: "M40 86l17 10-17 10z" }), "pop", 4.72, .3),
            s("rect", { class: "sp-on-fill", x: 14, y: 158, width: 48, height: 6, rx: 3, opacity: .7 }),
            s("rect", { class: "sp-on-fill", x: 14, y: 170, width: 32, height: 6, rx: 3, opacity: .5 })),
          "move", 4.22, .55, { x: "-86px", y: "0px" })),
      s("rect", { class: "sp-notch", x: 34, y: 12, width: 24, height: 5, rx: 2.5 })), "in", .2, .5)),
    gate(398, 151, 72, "GO", 3.3, 3.92),
    cursor(420, 158, 3.42, 3.92));
}

// ------------------------------------------------------------------ Relay Code
// Claude writes a change, hands it to Codex, gets a ✓ review back, and you merge
function code() {
  const rows = [["c", 0, 70], ["c", 12, 96], ["d", 12, 84], ["a", 12, 110], ["a", 24, 72], ["c", 12, 40], ["a", 0, 58]];
  const kids = [card(170, 132)];
  rows.forEach(([k, ind, w], i) => {
    const y = 15 + i * 15;
    if (k !== "c") kids.push(s("rect", { class: k === "a" ? "sp-add" : "sp-del", x: 6, y: y - 4, width: 158, height: 14, rx: 4 }),
                             s("rect", { class: k === "a" ? "sp-add-m" : "sp-del-m", x: 6, y: y - 4, width: 3, height: 14, rx: 1.5 }));
    kids.push(a(bar(18 + ind, y, Math.min(w, 140 - ind), 6), "grow", .5 + i * .07, .25));
  });
  kids.push(check(168, 2, 14, 2.12));
  const content = a(g(...kids), "rise", .3);
  const toCodex = a(g(content), "move", 1.28, .7, { x: "-342px", y: "0px" });
  const back = a(g(toCodex), "move", 2.5, .6, { x: "171px", y: "0px" });
  return scene(560, 300,
    a(wire("M56 96V150a12 12 0 0 0 12 12H195"), "draw", 2.95, .35),
    a(wire("M504 96V150a12 12 0 0 1 -12 12H365"), "draw", 2.95, .35),
    tile(24, 22, 64, "claude", 0), tile(472, 22, 64, "codex", .08),
    at(195, 96, back),
    gate(280, 263, 108, "Merge", 3.12, 3.72),
    cursor(318, 270, 3.22, 3.72));
}

// ------------------------------------------------------------------ Relay Council
// one question goes to three agents; three answers come back; the odd one out stands out; you decide
function council() {
  const ys = [50, 150, 250], logos = ["claude", "gemini", "codex"], answers = ["Yes", "Not yet", "Yes"];
  const kids = [];
  ys.forEach((y, i) => kids.push(a(wire(`M150 150L332 ${y}`), "draw", .5 + i * .08, .45)));
  kids.push(at(12, 112, a(g(card(138, 76, 16), text(18, 46, "sp-q", "Ship Friday?")), "rise", 0)));
  ys.forEach((y, i) => {
    const cy = (150 + y) / 2, dy = Math.round(80 / 182 * (y - 150));
    let chip;
    if (i === 1) {
      chip = g(a(s("rect", { class: "sp-ring", x: -51, y: -21, width: 102, height: 42, rx: 21 }), "pop", 2.36, .35),
               s("rect", { class: "sp-fill", x: -45, y: -15, width: 90, height: 30, rx: 15 }),
               text(0, 5.5, "sp-chip-hit", answers[i], "middle"),
               a(g(s("rect", { class: "sp-chip", x: -45, y: -15, width: 90, height: 30, rx: 15 }),
                   text(0, 5.5, "sp-chip-t", answers[i], "middle")), "out", 2.3, .3));
    } else {
      chip = g(s("rect", { class: "sp-chip", x: -36, y: -15, width: 72, height: 30, rx: 15 }),
               a(text(0, 5.5, "sp-chip-t", answers[i], "middle"), "dim", 2.3, .4));
    }
    kids.push(at(240, cy, a(g(chip), "glide", 1.25 + i * .14, .5, { x: "80px", y: dy + "px" })));
  });
  ys.forEach((y, i) => kids.push(tile(332, y - 26, 52, logos[i], .3 + i * .08)));
  kids.push(at(12, 212, a(g(s("circle", { class: "sp-person", cx: 15, cy: 15, r: 15 }),
                            s("circle", { class: "sp-ink", cx: 15, cy: 11.5, r: 4.6 }),
                            s("path", { class: "sp-ink", d: "M6.8 23.2a8.4 8.4 0 0 1 16.4 0z" }),
                            text(40, 20.5, "sp-you", "You decide")), "rise", 2.8)));
  return scene(400, 300, ...kids);
}

// ------------------------------------------------------------------ Relay Desk
// an email arrives, becomes a task, goes to an agent, the reply waits for your OK, then it's handled
function desk() {
  const row = () => g(card(168, 50), s("rect", { class: "sp-env", x: 12, y: 17, width: 22, height: 16, rx: 3 }),
                      s("path", { class: "sp-env", d: "M13.5 19l9.5 7 9.5-7" }),
                      bar(44, 15, 72, 7), bar(44, 29, 100, 6, "sp-ln2"));
  const old = (y, d) => at(12, y, a(g(a(g(row()), "move", .55, .4, { x: "0px", y: "-58px" })), "in", d, .4));
  return scene(400, 300,
    old(98, 0), old(156, .06),
    at(12, 40, a(g(row(),
                   a(s("circle", { class: "sp-dot", cx: 152, cy: 25, r: 5 }), "out", 4.3, .2),
                   check(152, 25, 9, 4.32)), "glide", .62, .45, { x: "-28px", y: "0px" })),
    ping(96, 65, 168, 50, 12, 1.05),
    a(wire("M180 65H279"), "draw", 1.2, .5),
    a(wire("M322 22a43 43 0 1 1 0 86a43 43 0 1 1 0 -86"), "draw", 2.0, .6),
    tile(290, 33, 64, "claude", .25),
    at(322, 65, a(g(s("rect", { class: "sp-card", x: -46, y: -17, width: 92, height: 34, rx: 10 }),
                    s("rect", { class: "sp-box", x: -35, y: -7, width: 14, height: 14, rx: 4 }),
                    bar(-12, -3.5, 46, 7)), "fly", 1.2, 1.0, { x: "-190px", y: "0px" })),
    at(240, 128, a(g(card(150, 72), bar(14, 15, 54, 7, "sp-accbar"), bar(14, 32, 118), bar(14, 47, 92)), "rise", 2.55)),
    ping(315, 247, 76, 38, 19, 3.25),
    gate(315, 247, 76, "OK", 3.0, 3.98),
    cursor(339, 254, 3.48, 3.98));
}

// ------------------------------------------------------------------ the section
const SPECIALS = [
  { key: "clips", word: "Clips", live: true, build: clips, end: 5.0, feature: true,
    promise: "From trend to posted clip. You just say go.",
    say: "A scout spots a trend, a writer scripts it and checks the facts, an editor cuts it, you press Go, and the clip is posted." },
  { key: "code", word: "Code", live: true, build: code, end: 4.2, feature: true, flip: true,
    promise: "Claude builds. Codex reviews. You merge.",
    say: "Claude writes a change and hands it to Codex, Codex sends it back approved, and you press Merge." },
  { key: "council", word: "Council", live: false, build: council, end: 3.3,
    promise: "Ask every AI. See where they disagree. You decide.",
    say: "One question goes to Claude, Gemini and Codex. Two say yes, one says not yet, and that answer is highlighted for you to decide." },
  { key: "desk", word: "Desk", live: false, build: desk, end: 4.7,
    promise: "Your inbox, handled, with your OK on what matters.",
    say: "A new email becomes a task for an agent, the reply waits for your OK, and once you give it the email is handled." },
];

function mark() {
  return s("svg", { class: "sp-mark", viewBox: "0 0 64 64", "aria-hidden": "true" },
           s("path", { d: "M30 10a22 22 0 0 0 0 44z" }), s("path", { d: "M38 10a22 22 0 0 1 0 44z", opacity: ".38" }));
}
function replayIcon() {
  return s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
           s("path", { d: "M19 12a7 7 0 1 1-2.05-4.95", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round" }),
           s("path", { d: "M19.5 4.5v4h-4", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" }));
}

function play(stage) {
  stage.classList.remove("sp-on", "sp-play");
  void stage.offsetWidth;                       // drop the old run so every animation starts over
  stage.classList.add("sp-on", "sp-play");
}

function item(sp) {
  const stage = h(motion ? "button" : "div", "sp-stage", sp.build(), a(h("span", "sp-replay", replayIcon()), "in", sp.end, .4));
  if (motion) stage.type = "button"; else stage.setAttribute("role", "img");
  stage.setAttribute("aria-label", sp.say + (motion ? " Play again." : ""));
  stage.dataset.special = sp.key;
  const name = h("h3", "sp-name", mark(), "Relay " + sp.word);
  const state = h("span", "sp-state " + (sp.live ? "is-live" : "is-soon"), sp.live ? "Available" : "Soon");
  const words = h("div", "sp-text", name, h("p", "sp-promise", sp.promise), state);
  const el = h("article", `sp-item sp-${sp.key}` + (sp.feature ? " sp-feature" : "") + (sp.flip ? " sp-flip" : ""), stage, words);
  if (motion) {
    stage.classList.add("sp-on");
    stage.addEventListener("click", () => play(stage));
  }
  return el;
}

const list = h("div", "sp-list");
const soon = h("div", "sp-pair");
for (const sp of SPECIALS) (sp.feature ? list : soon).append(item(sp));
list.append(soon);
root.append(list);

if (motion) {
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { io.unobserve(e.target); play(e.target); }
  }, { threshold: 0.55 });
  root.querySelectorAll(".sp-stage").forEach((st) => io.observe(st));
}
})();
