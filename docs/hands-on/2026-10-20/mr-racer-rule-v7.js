// MR Racer rule v7 (2026-10-20): v5 with a symmetric lane keeper: a line under the car with a lane line on one side
// only means the road edge is on the other, so it steers away from that edge; else it steers back to the middle.
// Clean run: 720 points at 5.8 km, stopped by hand (not crashed). retinat_reflex install, screen_width 480;
// rule 0 decides, the others hold keys:
(() => {
  const m = see.memory;
  m.t0 ??= see.t;
  const t = see.t - m.t0;
  const dark = (box) => { const a = see.locate([30, 25, 25], {from: 'screen', in: box, tol: 150}); return a ? a.n : 0; };
  const white = (box) => { const a = see.locate([255, 255, 255], {from: 'screen', in: box, tol: 90}); return a && a.n >= 3 ? a.x : null; };
  const lx = white([290, 268, 150, 30]), bx = white([440, 268, 180, 30]), rx = white([620, 268, 150, 30]);
  if (t < 9000) { m.steer = null; return false; }
  if (t < 9300) { m.steer = 'R'; return false; }
  if (m.dodge) {
    const crossed = m.dodge === 'R' ? (bx !== null && bx < 500) : (bx !== null && bx > 556);
    if (crossed || see.t > m.dodgeEnd) { m.dodge = null; } else { m.steer = m.dodge; return false; }
  }
  if (see.t < (m.until || 0)) return false;
  m.steer = null;
  m.brake = false;
  m.ahead = dark([500, 110, 60, 28]);
  const rClear = rx !== null && dark([556, 110, 56, 28]) < 8 && dark([590, 140, 110, 70]) < 40;
  const lClear = lx !== null && dark([448, 110, 56, 28]) < 8 && dark([360, 140, 110, 70]) < 150;
  if (m.ahead > 15) {
    if (see.t >= (m.calm || 0) && (rClear || lClear)) {
      m.dodge = rClear ? 'R' : 'L';
      m.steer = m.dodge;
      m.dodgeEnd = see.t + 900;
      m.calm = see.t + 1800;
      return false;
    }
    m.brake = true;
  }
  let s = null;
  if (bx !== null) {
    if (lx !== null && rx === null) s = 'L';
    else if (rx !== null && lx === null) s = 'R';
    else s = bx < 528 ? 'R' : 'L';
  }
  else if (lx !== null && lx < 355) s = 'L';
  else if (lx !== null && lx > 420) s = 'R';
  else if (rx !== null && rx < 660) s = 'L';
  else if (rx !== null && rx > 730) s = 'R';
  if (s) { m.steer = s; m.until = see.t + 60; }
  return false;
})()

/* key rules:
{"when": "!see.memory.brake", "hold": "ArrowUp"}
{"when": "see.memory.brake", "hold": "ArrowDown"}
{"when": "see.memory.steer === 'L'", "hold": "ArrowLeft"}
{"when": "see.memory.steer === 'R'", "hold": "ArrowRight"}
{"when": "see.t - (see.memory.t0 || see.t) > 5000 && (see.locate([22, 22, 22], {from: 'screen', in: [300, 100, 450, 200], tol: 40}) || {n: 0}).n > 12000", "notify": "results screen"}
*/
