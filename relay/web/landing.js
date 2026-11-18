// Landing page: draws the logo tiles and the contact sheet, then reveals each section once as it
// scrolls into view. It never talks to the hub, so it shows nothing about anyone's agents.
(function () {
"use strict";
const CURSOR = "M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z";
const NS = "http://www.w3.org/2000/svg";
function vec(d, viewBox, extra) {
  const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", viewBox); svg.setAttribute("aria-hidden", "true");
  for (const [path, op] of d) {
    const p = document.createElementNS(NS, "path"); p.setAttribute("d", path); p.setAttribute("fill", "currentColor");
    if (op) p.setAttribute("opacity", op);
    if (extra) p.setAttribute("fill-rule", "evenodd");
    svg.append(p);
  }
  return svg;
}
const markSvg = () => vec([["M30 10a22 22 0 0 0 0 44z"], ["M38 10a22 22 0 0 1 0 44z", ".38"]], "0 0 64 64");
function logo(src, full) {
  if (src === "cursor") return vec([[CURSOR]], "0 0 24 24", true);   // one colour, follows light/dark
  if (!src) return markSvg();
  const img = document.createElement("img"); img.src = src; img.alt = ""; if (full) img.className = "full"; return img;
}

// step 1: the app picker in miniature
const pick = document.getElementById("minipick");
for (const [src, full, sel] of [["/logos/claude.svg", false, true], ["/logos/codex.svg", true], ["/logos/muse.svg"],
                                 ["/logos/gemini.svg"], ["cursor"], [null]]) {
  const s = document.createElement("span"); if (sel) s.className = "sel"; s.append(logo(src, full)); pick.append(s);
}

// the apps with a guided setup
const apps = document.getElementById("apps-grid");
[["Claude", "/logos/claude.svg"], ["Claude Code", "/logos/claude.svg"], ["Codex", "/logos/codex.svg", true],
 ["Muse", "/logos/muse.svg"], ["Gemini CLI", "/logos/gemini.svg"], ["Cursor", "cursor"], ["Any MCP app", null]].forEach(([name, src, full], i) => {
  const ic = document.createElement("span"); ic.className = "ic"; ic.append(logo(src, full));
  const card = document.createElement("div"); card.className = "app"; card.style.animationDelay = `${i * 50}ms`;
  card.append(ic, document.createTextNode(name)); apps.append(card);
});

// a contact sheet that fills in shot by shot
const frames = document.getElementById("frames");
const tones = "aobxcoaboxdabocxoabxcoba";
[...tones].forEach((t, i) => {
  const f = document.createElement("i"); if ("abcd".includes(t)) f.className = t;
  f.style.animationDelay = `${120 + i * 35}ms`; frames.append(f);
});

const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
function countUp(el) {
  const to = Number(el.dataset.to), unit = el.dataset.unit || "", t0 = performance.now(), dur = 1100;
  const step = (now) => {
    const k = Math.min(1, (now - t0) / dur), eased = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(to * eased) + unit;
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
const items = document.querySelectorAll(".reveal");
if (!("IntersectionObserver" in window) || reduce) return;
document.documentElement.classList.add("js");      // hide-until-seen only once we know we can reveal
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) {
    e.target.classList.add("in"); io.unobserve(e.target);
    e.target.querySelectorAll("[data-to]").forEach(countUp);
  }
}, { threshold: 0.15 });
items.forEach((el, i) => { el.style.transitionDelay = `${(i % 3) * 80}ms`; io.observe(el); });
})();
