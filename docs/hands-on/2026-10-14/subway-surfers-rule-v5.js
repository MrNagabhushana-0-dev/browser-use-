(() => {
  const m = see.memory;
  const GROUND = [[172,147,96],[160,112,66],[205,130,30],[230,160,35],[187,123,45],[215,150,45],[205,190,115],[185,138,82],[220,161,41]];
  const K = (240 / 945) ** 2;
  const box = (o, y0, y1) => { const s = 0.5 * ((y0 + y1) / 2 - 80); const w = 0.5 * s; return [512 + o * s - w / 2, y0, w, y1 - y0]; };
  const ratio = (b) => GROUND.reduce((n, c) => { const a = see.locate(c, {from: 'screen', in: b, tol: 45}); return n + (a ? a.n : 0); }, 0) / Math.max(1, b[2] * b[3] * K);
  const near = [-1, 0, 1].map(o => ratio(box(o, 225, 260)));
  const far = [-1, 0, 1].map(o => ratio(box(o, 180, 220)));
  m.act = null;
  if (see.t < (m.until || 0)) return false;
  let red = 0;
  for (let y = 100; y < 190; y += 10) { const a = see.locate([196, 30, 26], {from: 'screen', in: [482, y, 60, 10], tol: 80}); red += a ? a.n : 0; }
  if (red > 60) { m.act = 'D'; m.until = see.t + 400; return false; }
  if (near[1] < 0.35 || far[1] < 0.4) {
    const ok = [0, 2].filter(i => near[i] > 0.45 && far[i] > 0.35).sort((a, b) => near[b] + far[b] - near[a] - far[a]);
    if (ok.length) { m.act = ok[0] === 0 ? 'L' : 'R'; m.until = see.t + 400; }
    else { m.act = 'U'; m.until = see.t + 400; }
  }
  return false;
})()
