/* The arc reactor.
 *
 * A separate file from app.js on purpose. app.js is the client of a real API —
 * it holds an access token, parses SSE frames by hand, and every DOM insertion
 * in it uses textContent because the Phase 0 audit found remote-data-to-innerHTML
 * in the old dashboard. None of that should have to share a file with a
 * decoration, and a decoration should not be able to break it: this module
 * touches one canvas, reads nothing, and sends nothing.
 *
 * It is also entirely optional. If the canvas is missing, or the context cannot
 * be acquired, this returns and the CSS-drawn mark stands in. A brand that
 * disappears when a script fails looks broken, so nothing here is load-bearing.
 */
(() => {
  "use strict";

  const canvas = document.getElementById("reactor");
  if (!canvas || !canvas.getContext) return;

  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  // Match the backing store to the device so the rings are not soft on a
  // retina display. Capped at 2: beyond that the cost is real and the
  // difference is not.
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const size = 22;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  ctx.scale(dpr, dpr);

  const still = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* One frame. `t` is a phase in turns, so the caller decides whether time
     passes — which is what makes the reduced-motion path a single call rather
     than a special case in here. */
  function draw(t) {
    const c = size / 2;
    ctx.clearRect(0, 0, size, size);

    // Outer ring.
    ctx.strokeStyle = "rgba(79,216,255,.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(c, c, 9.2, 0, Math.PI * 2);
    ctx.stroke();

    // Eight ticks, the reactor's coil housing.
    ctx.strokeStyle = "rgba(79,216,255,.75)";
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2 + t * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(c + Math.cos(a) * 6.4, c + Math.sin(a) * 6.4);
      ctx.lineTo(c + Math.cos(a) * 8.4, c + Math.sin(a) * 8.4);
      ctx.stroke();
    }

    // Inner ring, counter-rotating so the mark reads as two assemblies rather
    // than one spinning disc.
    ctx.strokeStyle = "rgba(79,216,255,.9)";
    ctx.beginPath();
    ctx.arc(c, c, 4.6, -t * Math.PI * 2, -t * Math.PI * 2 + Math.PI * 1.5);
    ctx.stroke();

    // Core.
    const glow = ctx.createRadialGradient(c, c, 0, c, c, 5);
    glow.addColorStop(0, "rgba(234,246,255,.95)");
    glow.addColorStop(0.45, "rgba(79,216,255,.75)");
    glow.addColorStop(1, "rgba(79,216,255,0)");
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(c, c, 5, 0, Math.PI * 2);
    ctx.fill();
  }

  let raf = 0;
  function start() {
    if (still.matches) {
      // Drawn once, at a fixed phase. Static, not blank: the mark is still the
      // mark for someone who has asked the page to hold still.
      cancelAnimationFrame(raf);
      draw(0);
      return;
    }
    const begin = performance.now();
    const tick = (now) => {
      draw(((now - begin) / 9000) % 1);
      raf = requestAnimationFrame(tick);
    };
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(tick);
  }

  // Honour a change of preference without a reload. addEventListener rather
  // than the deprecated addListener, with a guard for the older form because
  // this has to work on whatever browser the user already has open.
  if (still.addEventListener) still.addEventListener("change", start);
  else if (still.addListener) still.addListener(start);

  // Stop drawing when the tab is hidden. A background tab painting a canvas
  // forever is a laptop fan, and nobody is looking at it.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else start();
  });

  start();
})();
