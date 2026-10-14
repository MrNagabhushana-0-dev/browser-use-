(() => {
  const m = see.memory;
  const Y = [222, 178, 60];
  const keys = {U: [514, 297], D: [513, 335], L: [480, 335], R: [546, 335]};
  const n = {};
  for (const k in keys) { const a = see.locate(Y, {from: 'screen', in: [keys[k][0], keys[k][1], 30, 30], tol: 90}); n[k] = a ? a.n : 0; }
  const best = Object.keys(n).sort((a, b) => n[b] - n[a]);
  const lit = n[best[0]] >= 4 && n[best[0]] >= 2 * Math.max(1, n[best[1]]) ? best[0] : null;
  m.seen = lit && m.seenKey === lit ? (m.seen || 0) + 1 : (lit ? 1 : 0);
  m.seenKey = lit;
  m.act = m.seen >= 3 ? lit : null;
  return false;
})()
