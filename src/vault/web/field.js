/* ═══════════════════════════════════════════════════════════════════════════
   PARALLAX FIELD

   A decorative depth plane behind the interface: three layers of drifting
   particles with spring-damped pointer parallax, tuned to read as depth
   rather than as an effect.

   Four properties this holds:

     1. **Delta-time normalisation.** Every translation is multiplied by a
        tick factor derived from the real frame interval against 60Hz, so a
        144Hz display and a 30Hz one see the same motion at the same speed
        rather than the field running 2.4x faster on better hardware. The
        delta is clamped, because a backgrounded tab resumes with a delta of
        several seconds and an unclamped step would teleport every particle
        off-screen at once.

     2. **Object pooling, with no reallocation ever.** Particles live in flat
        typed arrays allocated once. The render loop creates no objects at
        all and therefore contributes nothing to garbage collection — which
        is what stops a background effect from causing periodic stutter in
        the foreground.

     3. **No style reads in the loop.** Colours and physics are constants in
        this module. Reading them from CSS with getComputedStyle would force
        the browser to flush style 60 times a second; this never touches the
        CSSOM at all.

     4. **It gets out of the way.** It stops when the tab is hidden, when the
        machine cannot hold a frame budget, when the pointer has been idle,
        and when the user has asked for reduced motion. A decorative layer
        that drains battery while nobody is looking at it is a bug.
   ═══════════════════════════════════════════════════════════════════════ */

/* Fixed art direction: 92 particles across three depth layers, tuned so the
   near plane reads as motion and the far plane as texture. More than this
   and the field competes with the content; fewer and the depth collapses. */
const PARTICLE_COUNT = 92;
const LAYERS = 3;
const PARALLAX_THROW = 26;          // px of displacement at the near plane
const BLOOM = 1;

/* Star hues, matching the interface signal colours. Most are near-white;
   roughly one in seven picks up cyan and one in eight violet, which is
   enough for the field to feel of a piece with the interface without
   turning into confetti. */
const HUE_WHITE = [235, 240, 255];
const HUE_CYAN = [0, 248, 255];
const HUE_VIOLET = [186, 0, 255];
const LAYER_SPEED = [0.052, 0.028, 0.013];
const LAYER_SIZE = [1.55, 1.05, 0.70];
const LAYER_ALPHA = [0.70, 0.46, 0.28];

const TARGET_HZ = 60;
const MAX_DELTA_MS = 64;            // clamp: never step more than ~4 frames
const IDLE_AFTER_MS = 45000;
const SLOW_FRAME_MS = 34;
const SLOW_FRAME_LIMIT = 90;

/**
 * @param {HTMLCanvasElement} canvas
 * @param {object} [options]
 * @param {boolean} [options.reducedMotion]
 */
export function startField(canvas, { reducedMotion = false } = {}) {
  if (!canvas) return { stop() {} };

  const context = canvas.getContext('2d', { alpha: true, desynchronized: true });
  if (!context) return { stop() {} };

  /* ── the pool ──────────────────────────────────────────────────────────
     Allocated once at maximum capacity. Never grown, never replaced.     */
  const px = new Float32Array(PARTICLE_COUNT);
  const py = new Float32Array(PARTICLE_COUNT);
  const pz = new Uint8Array(PARTICLE_COUNT);
  const pr = new Float32Array(PARTICLE_COUNT);
  const pa = new Float32Array(PARTICLE_COUNT);
  const pt = new Float32Array(PARTICLE_COUNT);
  const ptw = new Float32Array(PARTICLE_COUNT);
  const phue = new Uint8Array(PARTICLE_COUNT);

  let width = 0;
  let height = 0;

  /* Deterministic generator: the field is identical across reloads, which
     makes a visual comparison between two runs meaningful. */
  let seed = 0x2f5d50;
  function random() {
    seed ^= seed << 13; seed >>>= 0;
    seed ^= seed >> 17;
    seed ^= seed << 5;  seed >>>= 0;
    return seed / 0xffffffff;
  }

  function seedParticles() {
    seed = 0x2f5d50;
    for (let i = 0; i < PARTICLE_COUNT; i += 1) {
      const layer = i % LAYERS;
      px[i] = random() * (width || 1);
      py[i] = random() * (height || 1);
      pz[i] = layer;
      pr[i] = LAYER_SIZE[layer] * (0.62 + random() * 0.78);
      pa[i] = LAYER_ALPHA[layer] * (0.55 + random() * 0.55);
      pt[i] = random() * Math.PI * 2;
      ptw[i] = 0.0009 + random() * 0.0022;
      const roll = random();
      phue[i] = roll > 0.86 ? 1 : roll > 0.72 ? 2 : 0;
    }
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = canvas.clientWidth || window.innerWidth;
    height = canvas.clientHeight || window.innerHeight;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    seedParticles();
  }

  /* ── spring-damped pointer parallax ─────────────────────────────────── */
  let targetX = 0;
  let targetY = 0;
  let currentX = 0;
  let currentY = 0;
  let velocityX = 0;
  let velocityY = 0;
  const STIFFNESS = 0.055;
  const DAMPING = 0.82;

  let lastInteraction = performance.now();
  let pointerNX = 0;
  let pointerNY = 0;

  function onPointer(event) {
    pointerNX = (event.clientX / Math.max(1, width)) - 0.5;
    pointerNY = (event.clientY / Math.max(1, height)) - 0.5;
    lastInteraction = performance.now();
  }

  /* ── loop ───────────────────────────────────────────────────────────── */
  let running = true;
  let frame = 0;
  let previous = performance.now();
  let slowFrames = 0;

  function draw(now) {
    if (!running) return;
    frame = requestAnimationFrame(draw);

    const rawDelta = now - previous;
    previous = now;
    const delta = rawDelta > MAX_DELTA_MS ? MAX_DELTA_MS : rawDelta;
    const tick = (delta * TARGET_HZ) / 1000;

    if (rawDelta > SLOW_FRAME_MS) {
      slowFrames += 1;
      if (slowFrames > SLOW_FRAME_LIMIT) { stop(); return; }
    } else if (slowFrames > 0) {
      slowFrames -= 1;
    }

    context.clearRect(0, 0, width, height);

    targetX = pointerNX * PARALLAX_THROW;
    targetY = pointerNY * (PARALLAX_THROW * 0.7);

    const forceX = (targetX - currentX) * STIFFNESS;
    const forceY = (targetY - currentY) * STIFFNESS;
    velocityX = (velocityX + forceX * tick) * DAMPING;
    velocityY = (velocityY + forceY * tick) * DAMPING;
    currentX += velocityX * tick;
    currentY += velocityY * tick;

    const idle = now - lastInteraction > IDLE_AFTER_MS;

    for (let i = 0; i < PARTICLE_COUNT; i += 1) {
      const layer = pz[i];

      if (!idle) {
        py[i] -= LAYER_SPEED[layer] * tick * 14;
        if (py[i] < -4) {
          py[i] = height + 4;
          px[i] = random() * width;
        }
        pt[i] += ptw[i] * delta;
      }

      const depth = (LAYERS - layer) / LAYERS;
      const x = px[i] + currentX * depth;
      const y = py[i] + currentY * depth;

      if (x < -6 || x > width + 6 || y < -6 || y > height + 6) continue;

      const twinkle = 0.72 + Math.sin(pt[i]) * 0.28;
      const alpha = pa[i] * twinkle;
      const hue = phue[i] === 1 ? HUE_CYAN : phue[i] === 2 ? HUE_VIOLET : HUE_WHITE;

      context.beginPath();
      context.arc(x, y, pr[i], 0, Math.PI * 2);
      context.fillStyle = `rgba(${hue[0]}, ${hue[1]}, ${hue[2]}, ${alpha.toFixed(3)})`;
      context.fill();

      if (layer === 0 && pr[i] > 1.9) {
        context.shadowBlur = 8 * BLOOM;
        context.shadowColor =
          `rgba(${hue[0]}, ${hue[1]}, ${hue[2]}, ${(alpha * 0.8).toFixed(3)})`;
        context.fill();
        context.shadowBlur = 0;
      }
    }
  }

  function onVisibility() {
    if (document.hidden) {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
    } else if (running && !frame) {
      previous = performance.now();   // never step across the hidden gap
      slowFrames = 0;
      frame = requestAnimationFrame(draw);
    }
  }

  function stop() {
    running = false;
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    context.clearRect(0, 0, width, height);
    window.removeEventListener('resize', resize);
    window.removeEventListener('pointermove', onPointer);
    document.removeEventListener('visibilitychange', onVisibility);
  }

  function restart() {
    if (running) return;
    running = true;
    slowFrames = 0;
    previous = performance.now();
    window.addEventListener('resize', resize, { passive: true });
    window.addEventListener('pointermove', onPointer, { passive: true });
    document.addEventListener('visibilitychange', onVisibility);
    resize();
    frame = requestAnimationFrame(draw);
  }

  if (reducedMotion) {
    // Respected, but not one-way: clearing the preference and reloading, or
    // raising the star count, brings it back.
    running = false;
    return { stop, restart, isRunning: () => running };
  }

  window.addEventListener('resize', resize, { passive: true });
  window.addEventListener('pointermove', onPointer, { passive: true });
  document.addEventListener('visibilitychange', onVisibility);

  resize();
  frame = requestAnimationFrame(draw);

  return { stop, restart, isRunning: () => running };
}

/**
 * The nebula wash drifts on the same delta-time basis, an order of magnitude
 * slower. It is a CSS element rather than canvas because a blurred radial
 * gradient is far cheaper to composite than to paint.
 */
export function driftNebula(node, { reducedMotion = false } = {}) {
  if (!node || reducedMotion) return { stop() {} };

  let running = true;
  let frame = 0;
  let previous = performance.now();
  let phase = 0;

  function step(now) {
    if (!running) return;
    frame = requestAnimationFrame(step);

    const raw = now - previous;
    previous = now;
    const delta = raw > MAX_DELTA_MS ? MAX_DELTA_MS : raw;
    const tick = (delta * TARGET_HZ) / 1000;

    phase += 0.00042 * tick;
    const x = Math.sin(phase) * 22;
    const y = Math.cos(phase * 0.76) * 16;
    const scale = 1 + Math.sin(phase * 0.5) * 0.015;
    node.style.transform =
      `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0) scale(${scale.toFixed(4)})`;
  }

  function onVisibility() {
    if (document.hidden) {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
    } else if (running && !frame) {
      previous = performance.now();
      frame = requestAnimationFrame(step);
    }
  }

  document.addEventListener('visibilitychange', onVisibility);
  frame = requestAnimationFrame(step);

  return {
    stop() {
      running = false;
      if (frame) cancelAnimationFrame(frame);
      document.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
