(() => {
  const m = see.memory;
  const lanes = {L: [395, 75], C: [470, 85], R: [555, 75]};
  const n = {};
  for (const k in lanes) { const a = see.locate([30, 25, 25], {from: 'screen', in: [lanes[k][0], 88, lanes[k][1], 36], tol: 150}); n[k] = a ? a.n : 0; }
  m.n = n;
  if (see.t < (m.until || 0)) return false;
  m.steer = null;
  if (n.C > 30) {
    const side = n.L <= n.R ? 'L' : 'R';
    if (n[side] < n.C / 2) { m.steer = side; m.until = see.t + 450; m.moves = (m.moves || 0) + 1; }
  }
  return false;
})()
