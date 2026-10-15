(() => {
  const m = see.memory;
  m.n = (m.n || 0) + 1;
  const GROUND = [[172,147,96],[160,112,66],[205,130,30],[230,160,35],[187,123,45],[215,150,45]];
  const VX = 497, VY = 116, PY = 326, LANE = 146;
  const ground = (box) => GROUND.reduce((s, c) => { const a = see.locate(c, {from: 'screen', in: box, tol: 40}); return s + (a ? a.n : 0); }, 0);
  const box = (off, y0, y1) => { const f = ((y0 + y1) / 2 - VY) / (PY - VY); const cx = 520 + off * LANE * f - (520 - VX) * (1 - f); const w = 70 * f; return [cx - w / 2, y0, w, y1 - y0]; };
  const lanes = (y0, y1) => [-1, 0, 1].map(o => ground(box(o, y0, y1)));
  const near = lanes(225, 275), far = lanes(175, 225);
  m.near = near; m.far = far;
  m.base = Math.max((m.base || 0) * 0.995, near[1], 1);
  const clear = (i) => near[i] > m.base * 0.45 && far[i] > m.base * 0.15;
  m.act = null;
  if (see.t < (m.until || 0)) return false;
  if (near[1] < m.base * 0.3) {
    const opts = [0, 2].filter(clear).sort((a, b) => (near[b] + far[b]) - (near[a] + far[a]));
    if (opts.length) { m.act = opts[0] === 0 ? 'L' : 'R'; m.until = see.t + 450; }
    else { m.act = 'U'; m.until = see.t + 700; }
    m.acts = (m.acts || 0) + 1;
  }
  return false;
})()
