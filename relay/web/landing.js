// Landing page: replays a real recorded Relay run (hero-run.js, generated from hero-run.json) as a
// chat. Every line, number, file and timing comes from that run; the replay only speeds it up.
// It never talks to the hub, so it shows nothing about anyone's agents.
(function () {
"use strict";
const RUN = window.RELAY_HERO_RUN;
const log = document.getElementById("log");
if (!RUN || !log) return;
const SPEED = 3;                 // real seconds per replay second
const LEAD = 0.6;                // replay seconds before the first message
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
const NS = "http://www.w3.org/2000/svg";
const AVATAR = { claude: "/logos/claude.svg", muse: "/logos/muse.svg" };
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
const videoIcon = (stroke) => svg("0 0 24 24", [["rect", { x: 3, y: 5, width: 18, height: 14, rx: 3 }], ["path", { d: "M10 9.5v5l4.5-2.5z", fill: stroke, stroke: "none" }]],
  { fill: "none", stroke, "stroke-width": "1.8" });
function withMentions(parent, text) {          // "@claude" in bold, the rest as plain text
  for (const part of text.split(/(@[a-z0-9._-]+)/)) {
    if (!part) continue;
    parent.append(part.startsWith("@") ? el("span", "at", part) : document.createTextNode(part));
  }
}
const mb = (bytes) => `${Math.round(bytes / 1e6)} MB`;
const secs = (s) => `${s.toFixed(1)} s`;

// ---------------------------------------------------------------- the run, as a timeline
const chat = RUN.messages.filter((m) => m.from === "you" || (m.by === "script" && !m.job));
const jobs = RUN.jobs;
const jobFor = (m) => jobs.filter((j) => j.done <= m.t).pop();        // the job this reply reports on

// Build every row up front, in its final shape, so the page never shifts while it plays.
const rows = chat.map((m) => {
  const row = el("li", m.from === "you" ? "row me" : "row");
  const b = el("div", "b");
  withMentions(b, m.text);
  const step = { m, row, b, at: m.t };
  if (m.from === "you") {
    if (m.t === 0) {
      const src = RUN.source, file = el("div", "file");
      file.append(videoIcon("currentColor"), document.createTextNode(`${src.name} · ${mb(src.bytes)}`));
      b.append(file);
    }
    row.append(b);
    return step;
  }
  const av = el("span", "av"), img = el("img"); img.src = AVATAR[m.from] || "/static/mark.svg"; img.alt = ""; av.append(img);
  const body = el("div", "body"), who = el("div", "who"), st = el("span", "st");
  who.append(el("b", null, m.from), st);
  body.append(who, b);
  row.append(av, body);
  step.st = st;
  const job = jobFor(m);
  if (m.from === "claude" && job) {
    step.job = job;
    step.from = job.queued;                                             // the row opens when the work is handed off
    if (m.files[0] === RUN.digest.sheet) {
      const im = el("img", "sheet"); im.src = "/static/" + RUN.digest.sheet; im.alt = `Contact sheet: one frame from each of the ${RUN.digest.shots} shots`;
      [im.width, im.height] = RUN.digest.sheet_size; im.decoding = "async";
      b.append(im);
    } else {
      const grid = el("div", "clips");
      step.videos = RUN.clips.map((c, i) => {
        const btn = el("button", "clip idle"); btn.type = "button";
        btn.setAttribute("aria-label", `Play clip ${i + 1}, ${c.seconds} seconds from ${Math.floor(c.start / 60)}:${String(Math.floor(c.start % 60)).padStart(2, "0")}`);
        const v = document.createElement("video");
        v.muted = true; v.defaultMuted = true; v.playsInline = true;
        v.setAttribute("muted", ""); v.setAttribute("playsinline", ""); v.setAttribute("preload", "metadata");
        v.poster = "/static/" + c.poster; v.src = "/static/" + c.file;
        v.disablePictureInPicture = true;
        const bar = el("span", "bar"), fill = el("i"); bar.append(fill);
        const play = el("span", "play"); play.append(svg("0 0 24 24", [["path", { d: "M7 4.5v15l13-7.5z", fill: "#fff" }]]));
        btn.append(v, el("span", "n", String(i + 1)), bar, play);
        const paint = () => { fill.style.transform = `scaleX(${v.duration ? Math.min(1, v.currentTime / v.duration) : 0})`; };
        v.addEventListener("timeupdate", paint);
        v.addEventListener("playing", () => btn.classList.remove("idle"));
        v.addEventListener("ended", () => { paint(); btn.classList.add("idle"); });
        btn.addEventListener("click", () => {                       // a tap plays it again, from the top
          if (!v.paused && !v.ended) { v.pause(); btn.classList.add("idle"); return; }
          if (v.ended) v.currentTime = 0;
          v.play().catch(() => btn.classList.add("idle"));
        });
        grid.append(btn);
        return v;
      });
      const credit = el("div", "credit");
      const a = el("a", null, "Big Buck Bunny"); a.href = RUN.source.credit_url; a.rel = "noopener"; a.target = "_blank";
      credit.append("Video: ", a, " © Blender Foundation, CC BY 3.0");
      b.append(grid, credit);
    }
  } else if (m.from === "muse") {
    const go = el("button", "go", "Go"); go.type = "button";
    const ok = el("span", "okay");
    ok.append(svg("0 0 24 24", [["circle", { cx: 12, cy: 12, r: 11, fill: "var(--green)" }], ["path", { d: "M7 12.5l3.2 3.2L17 9", fill: "none", stroke: "var(--on-green)", "stroke-width": "2.4", "stroke-linecap": "round", "stroke-linejoin": "round" }]]),
      document.createTextNode("Approved. It's a replay, so nothing was posted."));
    go.addEventListener("click", () => { b.classList.add("approved"); step.st.textContent = "approved"; step.st.className = "st done"; });
    b.append(go, ok);
    step.ask = true;
  }
  return step;
});
rows.forEach((s) => log.append(s.row));

// ---------------------------------------------------------------- playing it
let timers = [], frames = new Set(), frame = 0, playing = false;
const later = (ms, fn) => timers.push(setTimeout(fn, ms));
const T = (t) => (LEAD + t / SPEED) * 1000;

function finalState(s) {
  s.row.classList.remove("later"); s.b.classList.remove("hold");
  if (s.job) { s.st.textContent = `done in ${secs(s.job.seconds)} by @${s.job.by}`; s.st.className = "st done"; }
  if (s.ask) { s.st.textContent = "needs you"; s.st.className = "st ask"; }
}
function reset() {
  timers.forEach(clearTimeout); timers = []; cancelAnimationFrame(frame); frames.forEach(cancelAnimationFrame); frames.clear();
  for (const s of rows) {
    s.row.className = s.m.from === "you" ? "row me later" : "row later";
    s.b.className = "b hold"; s.b.classList.remove("approved");
    if (s.st) { s.st.textContent = ""; s.st.className = "st"; }
    for (const v of s.videos || []) { v.pause(); try { v.currentTime = 0; } catch (e) { /* not loaded yet */ } v.dispatchEvent(new Event("timeupdate")); v.parentElement.classList.add("idle"); }
  }
}
function counter(s) {                                  // "studio is on it · 4.2 s", counting real seconds
  const begin = performance.now();
  const tick = (now) => {
    if (!s.b.classList.contains("hold")) return;
    const real = Math.min(s.job.seconds, ((now - begin) / 1000) * SPEED);
    s.st.textContent = `@${s.job.by} is on it · ${secs(real)}`;
    frames.add(requestAnimationFrame(tick));
  };
  frames.add(requestAnimationFrame(tick));
}
function playWhenSeen(videos) {                         // play once, when they're actually on screen
  const go = () => videos.forEach((v) => {
    v.muted = true;
    const p = v.play();
    if (p) p.catch(() => v.parentElement.classList.add("idle"));
  });
  if (!("IntersectionObserver" in window)) return go();
  const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { io.disconnect(); go(); } }, { threshold: 0.5 });
  io.observe(videos[0].parentElement);
}
function play() {
  reset(); playing = true;
  label.textContent = `Playing at ${SPEED}×`;
  let prevEnd = 0;
  for (const s of rows) {
    const show = T(s.m.t);
    if (s.m.from === "you") {
      later(show, () => { s.row.classList.replace("later", "show"); s.b.classList.remove("hold"); });
    } else if (s.job) {
      later(T(s.from), () => { s.row.classList.replace("later", "show"); counter(s); });
      later(show, () => {
        s.b.classList.replace("hold", "show"); finalState(s);
        if (s.videos) playWhenSeen(s.videos);
      });
    } else {
      later(show, () => { s.row.classList.replace("later", "show"); s.b.classList.replace("hold", "show"); finalState(s); });
    }
    prevEnd = show;
  }
  later(prevEnd + 900, () => { playing = false; label.textContent = "Tap below to play it again"; chip.setAttribute("aria-label", "Replay the run: cut clips from a video"); });
}
function still() {                                     // reduced motion: the whole exchange, at rest; videos play on tap
  reset();
  for (const s of rows) finalState(s);
  label.textContent = "Ready";
}

chip.addEventListener("click", () => {
  document.getElementById("chat").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  if (reduce) still(); else if (!playing) play();
});
if (reduce) { still(); return; }
reset();
// start when the chat is in view (it usually is, straight away)
if (!("IntersectionObserver" in window)) { play(); return; }
const seen = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { seen.disconnect(); play(); } }, { threshold: 0.2 });
seen.observe(log);
})();
