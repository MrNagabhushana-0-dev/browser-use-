(() => {
  const m = see.memory;
  m.t0 ??= see.t;
  const t = see.t - m.t0;
  if (t > 9000 && t < 9300) { m.steer = 'R'; m.lane = 1; return false; }
  if (t < 9300) { m.steer = null; return false; }
  if (see.t < (m.until || 0)) return false;
  m.steer = null;
  if (see.t < (m.calm || 0)) return false;
  const a = see.locate([30, 25, 25], {from: 'screen', in: [488, 108, 80, 28], tol: 150});
  const n = a ? a.n : 0;
  m.n = n;
  if (n > 15) {
    m.steer = m.lane === 1 ? 'R' : 'L';
    m.lane = m.lane === 1 ? 2 : 1;
    m.until = see.t + 300;
    m.calm = see.t + 1500;
    m.dodges = (m.dodges || 0) + 1;
  }
  return false;
})()
