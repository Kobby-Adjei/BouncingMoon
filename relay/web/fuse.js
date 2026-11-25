// Two halves become one, with physics instead of keyframes. Shared by the landing page and the app.
// Everything is simulated once, then played back as one animation:
//   thrown in   each half flies in from its side, tilted, on a damped spring (never through the other)
//   pulled      a pull that grows as they get closer, like magnets
//   the knock   they hit, bounce a little and squash along the seam, then stick
//   the spin    the knock sets the whole orb turning; momentum, then friction, one full turn
//   the seal    drops in, overshoots and settles, and its weight presses the orb down a touch
// relayFuse(orb, options) returns the milliseconds until the seal has settled, or 0 when it didn't run
// (reduced motion, or no Web Animations): the page's own styles show the sealed state then.
(function () {
"use strict";
const DT = 1 / 240, FRAME = 1 / 60;
const spring = (x, v, target, k, c) => { v += (-k * (x - target) - c * v) * DT; return [x + v * DT, v]; };
const ramp = (t, a, b) => Math.min(1, Math.max(0, (t - a) / (b - a)));
const smooth = (t) => t * t * (3 - 2 * t);

window.relayFuse = function (orb, o) {
  if (!orb || !orb.animate || matchMedia("(prefers-reduced-motion: reduce)").matches) return 0;
  const L = orb.querySelector(o.left), R = orb.querySelector(o.right), seal = orb.querySelector(o.seal);
  const marks = [...orb.querySelectorAll(o.marks)];
  const S = orb.getBoundingClientRect().width || 200;
  const rest = o.markRest || 0;                         // how much of the logos stays visible under the seal
  const base = o.sealBase || "";                       // how the page centres the seal, kept under the motion
  const gap0 = o.gap != null ? o.gap : 0.06 * S;       // how far apart the halves rest before the pull
  const enter = !!o.enter;

  const half = (dir, delay) => enter ? { dir, delay, x: dir * 0.55 * S, vx: dir * -0.6 * S, r: dir * 26, vr: 0 }
                                     : { dir, delay: 0, x: 0, vx: 0, r: 0, vr: 0 };
  const halves = [half(-1, 0.08), half(1, 0.17)];
  const pullAt = enter ? 0.9 : 0.05;
  let g = gap0, vg = 0, stuck = false, contact = null;
  let q = 0, vq = 0, th = 0, vth = 0, oy = 0, voy = 0;
  let ss = 0.12, vss = 0, sy = -0.22 * S, vsy = 0, sealAt = null, landed = false, settled = null;
  const F = { L: [], R: [], orb: [], marks: [], seal: [] };

  let t = 0, next = 0, end = Infinity;
  while (t < end && t < 6) {
    for (const hf of halves) {
      if (t < hf.delay) continue;
      [hf.x, hf.vx] = spring(hf.x, hf.vx, 0, 150, 19.5);
      [hf.r, hf.vr] = spring(hf.r, hf.vr, 0, 115, 17);
    }
    if (t >= pullAt && !stuck) {
      vg += -9 * S * (1 + 2.2 * (1 - Math.min(1, g / Math.max(gap0, 1)))) * DT;
      g += vg * DT;
      if (g <= 0) {
        g = 0;
        const hit = Math.abs(vg);
        vq += 0.0042 * hit;
        if (contact === null) { contact = t; vth = 760; sealAt = t + 0.78; }
        vg = hit * 0.3;
        if (hit < 0.14 * S) { vg = 0; stuck = true; }
      }
    }
    [q, vq] = spring(q, vq, 0, 950, 17);
    if (contact !== null) [th, vth] = spring(th, vth, 360, 26, 9.4);
    if (sealAt !== null && t >= sealAt) {
      [ss, vss] = spring(ss, vss, 1, 430, 15);
      [sy, vsy] = spring(sy, vsy, 0, 330, 21);
      if (!landed && sy > -0.01 * S) { landed = true; voy += 0.3 * S; }
      if (settled === null && Math.abs(ss - 1) < 0.02 && Math.abs(sy) < 0.5) settled = t;
    }
    [oy, voy] = spring(oy, voy, 0, 520, 23);
    if (t >= next) {
      next += FRAME;
      const fade = (hf) => enter ? smooth(ramp(t, hf.delay, hf.delay + 0.2)) : 1;
      F.L.push({ transform: `translateX(${(halves[0].x - g / 2).toFixed(2)}px) rotate(${halves[0].r.toFixed(2)}deg)`, opacity: fade(halves[0]) });
      F.R.push({ transform: `translateX(${(halves[1].x + g / 2).toFixed(2)}px) rotate(${halves[1].r.toFixed(2)}deg)`, opacity: fade(halves[1]) });
      F.orb.push({ transform: `translateY(${oy.toFixed(2)}px) rotate(${th.toFixed(2)}deg) scale(${(1 - q).toFixed(4)}, ${(1 + q * 0.55).toFixed(4)})` });
      F.marks.push({ opacity: contact === null ? 1 : 1 - (1 - rest) * smooth(ramp(t, contact + 0.12, contact + 0.5)) });
      F.seal.push({ transform: `${base} translateY(${sy.toFixed(2)}px) scale(${Math.max(0, ss).toFixed(4)})`.trim(),
                    opacity: sealAt !== null && t >= sealAt ? smooth(ramp(t, sealAt, sealAt + 0.08)) : 0 });
    }
    if (landed && end === Infinity && Math.abs(ss - 1) < 0.002 && Math.abs(vss) < 0.02 && Math.abs(th - 360) < 0.05 && Math.abs(oy) < 0.05) end = t + 0.05;
    t += DT;
  }
  // the last frame is exactly the resting state: the halves together, still, the seal centred
  F.L.push({ transform: "none", opacity: 1 }); F.R.push({ transform: "none", opacity: 1 });
  F.orb.push({ transform: "none" }); F.marks.push({ opacity: rest }); F.seal.push({ transform: base || "none", opacity: 1 });
  const run = { duration: F.orb.length * FRAME * 1000, easing: "linear", fill: "both" };
  L.animate(F.L, run); R.animate(F.R, run); orb.animate(F.orb, run); seal.animate(F.seal, run);
  for (const m of marks) m.animate(F.marks, run);
  return Math.round(((settled || t)) * 1000);
};
})();
