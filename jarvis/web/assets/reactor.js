/* The status reactor.
 *
 * A large concentric-ring display at the head of the command centre. This is
 * the single biggest visual borrowing from the films, and it is here on one
 * condition: it carries state. A decorative ring that spins regardless would
 * be a lie the size of the panel — the most prominent thing on screen saying
 * nothing.
 *
 * So each ring is a real reading:
 *
 *   outer   pending approvals      gold, and it fills. Nothing else on the
 *                                  page is gold at rest, so a gold arc means
 *                                  a human is needed.
 *   middle  running background jobs cyan, one segment per job
 *   inner   refusals in 24h        red, and it is *supposed* to be empty
 *   core    the mode               the only thing that changes colour wholesale
 *
 * Reading it wrong must not be possible from colour alone, so every ring has a
 * printed number beside it in the panel. This is reinforcement, not the only
 * signal — the same rule the rest of the interface follows.
 */
(() => {
  "use strict";

  const canvas = document.getElementById("statusReactor");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const SIZE = 180;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = SIZE * dpr;
  canvas.height = SIZE * dpr;
  ctx.scale(dpr, dpr);

  const still = window.matchMedia("(prefers-reduced-motion: reduce)");

  const MODE_COLOUR = {
    SAFE: "#4fd8ff",
    ASSISTED: "#4fd8ff",
    AUTONOMOUS: "#ffb545",
    LOCKDOWN: "#ff5d5d",
  };

  let reading = { approvals: 0, jobs: 0, denials: 0, mode: "SAFE" };
  let raf = 0;

  function arc(radius, from, to, colour, width) {
    ctx.strokeStyle = colour;
    ctx.lineWidth = width;
    ctx.lineCap = "butt";
    ctx.beginPath();
    ctx.arc(SIZE / 2, SIZE / 2, radius, from, to);
    ctx.stroke();
  }

  /* `spin` is a phase in turns. Passing 0 draws the still frame, which is what
     makes the reduced-motion path one call rather than a branch inside here. */
  function draw(spin) {
    const c = SIZE / 2;
    const TAU = Math.PI * 2;
    const top = -Math.PI / 2;
    ctx.clearRect(0, 0, SIZE, SIZE);

    // Ring tracks. Drawn first and dim, so an empty reading still reads as an
    // instrument at zero rather than as a broken one.
    arc(74, 0, TAU, "rgba(79,216,255,.10)", 6);
    arc(58, 0, TAU, "rgba(79,216,255,.10)", 6);
    arc(42, 0, TAU, "rgba(79,216,255,.10)", 6);

    // Outer: approvals. Capped at five, because past that the arc says
    // "several" either way and a full circle is clearer than a crowded one.
    if (reading.approvals > 0) {
      const share = Math.min(reading.approvals, 5) / 5;
      arc(74, top, top + TAU * share, "#ffb545", 6);
    }

    // Middle: running jobs, one segment each with a gap between, so three jobs
    // is countable rather than merely "some".
    const jobs = Math.min(reading.jobs, 6);
    for (let i = 0; i < jobs; i++) {
      const step = TAU / 6;
      arc(58, top + i * step + 0.06, top + (i + 1) * step - 0.06, "#4fd8ff", 6);
    }

    // Inner: refusals. Empty is the healthy state and it should look it.
    if (reading.denials > 0) {
      const share = Math.min(reading.denials, 10) / 10;
      arc(42, top, top + TAU * share, "#ff5d5d", 6);
    }

    // A slow sweep, decorative, over the outermost track only.
    if (spin) {
      arc(84, top + TAU * spin, top + TAU * spin + 0.5,
          "rgba(79,216,255,.55)", 1);
    }

    // Core.
    const colour = MODE_COLOUR[reading.mode] || MODE_COLOUR.SAFE;
    const glow = ctx.createRadialGradient(c, c, 0, c, c, 30);
    glow.addColorStop(0, "rgba(234,246,255,.95)");
    glow.addColorStop(0.35, colour);
    glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(c, c, 30, 0, TAU);
    ctx.fill();

    ctx.strokeStyle = colour;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(c, c, 24, 0, TAU);
    ctx.stroke();
  }

  function start() {
    cancelAnimationFrame(raf);
    if (still.matches) {
      draw(0);
      return;
    }
    const begin = performance.now();
    const tick = (now) => {
      draw(((now - begin) / 12000) % 1);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
  }

  // The console owns the numbers; this owns the drawing. Kept apart so a
  // rendering bug cannot take the panel's data with it.
  document.addEventListener("jarvis:reading", (event) => {
    if (event.detail) reading = Object.assign(reading, event.detail);
    if (still.matches) draw(0);
  });

  if (still.addEventListener) still.addEventListener("change", start);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else start();
  });

  start();
})();
