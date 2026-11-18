// Landing page: reveal sections once as they scroll into view, and draw the app chips.
// It never calls the hub's API, so it shows nothing about anyone's agents.
(function () {
"use strict";
const CURSOR = "M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z";
const chips = [
  ["Claude", "/logos/claude.svg"], ["Claude Code", "/logos/claude.svg"], ["Codex", "/logos/codex.svg", true],
  ["Muse", "/logos/muse.svg"], ["Gemini CLI", "/logos/gemini.svg"], ["Cursor", "cursor"], ["Any MCP app", null],
];
const box = document.getElementById("appchips");
for (const [name, src, full] of chips) {
  const chip = document.createElement("span"); chip.className = "appchip";
  const ic = document.createElement("span"); ic.className = "ic";
  if (src === "cursor") {         // Cursor's mark is one colour, drawn inline so it follows light/dark
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 24 24");
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", CURSOR);
    p.setAttribute("fill", "currentColor"); p.setAttribute("fill-rule", "evenodd"); svg.append(p); ic.append(svg);
  } else if (src) { const img = document.createElement("img"); img.src = src; img.alt = ""; if (full) img.className = "full"; ic.append(img); }
  else {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 64 64");
    for (const [d, op] of [["M30 10a22 22 0 0 0 0 44z", "1"], ["M38 10a22 22 0 0 1 0 44z", ".38"]]) {
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", d);
      p.setAttribute("fill", "currentColor"); p.setAttribute("opacity", op); svg.append(p);
    }
    ic.append(svg);
  }
  chip.append(ic, document.createTextNode(name));
  box.append(chip);
}
const items = document.querySelectorAll(".reveal");
if (!("IntersectionObserver" in window) || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
document.documentElement.classList.add("js");      // hide-until-seen only once we know we can reveal
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
}, { threshold: 0.12 });
items.forEach((el, i) => { el.style.transitionDelay = `${(i % 3) * 70}ms`; io.observe(el); });
})();
