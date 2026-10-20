// MR Racer rule v4 (2026-10-20): v3 plus lane keeping by the white lines below the car. Score 611 at 4.0 km (pinned
// to the right barrier at 30 kph for long stretches: the keeper read the edge line under the car as "line to the left").
// retinat_reflex install, screen_width 480; rule 0 decides, the others hold keys:
(() => {
  const m = see.memory;
  m.t0 ??= see.t;
  const t = see.t - m.t0;
  const dark = (box) => { const a = see.locate([30, 25, 25], {from: 'screen', in: box, tol: 150}); return a ? a.n : 0; };
  const white = (box) => { const a = see.locate([255, 255, 255], {from: 'screen', in: box, tol: 90}); return a && a.n >= 3 ? a.x : null; };
  const lx = white([300, 268, 230, 30]), rx = white([530, 268, 230, 30]);
  m.lx = lx; m.rx = rx;
  if (t < 9000) { m.steer = null; return false; }
  if (t < 9300) { m.steer = 'R'; m.lane = 0; return false; }
  if (m.dodge) {
    const crossed = m.dodge === 'R' ? (lx !== null && lx > 440) : (rx !== null && rx < 620);
    if (crossed || see.t > m.dodgeEnd) { m.dodge = null; } else { m.steer = m.dodge; return false; }
  }
  if (see.t < (m.until || 0)) return false;
  m.steer = null;
  m.brake = false;
  m.ahead = dark([500, 110, 60, 28]);
  const rClear = dark([556, 110, 56, 28]) < 8 && dark([590, 140, 110, 70]) < 40;
  const lClear = dark([448, 110, 56, 28]) < 8 && dark([360, 140, 110, 70]) < 150;
  if (m.ahead > 15) {
    if (see.t >= (m.calm || 0)) {
      const goR = m.lane < 1 && rClear;
      const goL = m.lane > -2 && lClear;
      if (goR || goL) {
        m.dodge = goR ? 'R' : 'L';
        m.steer = m.dodge;
        m.lane += goR ? 1 : -1;
        m.dodgeEnd = see.t + 900;
        m.calm = see.t + 1800;
        m.moves = (m.moves || 0) + 1;
        return false;
      }
    }
    m.brake = true;
  }
  if (lx !== null && lx > 440) { m.steer = 'R'; m.until = see.t + 60; }
  else if (rx !== null && rx < 620) { m.steer = 'L'; m.until = see.t + 60; }
  return false;
})()
/* key rules:
{"when": "!see.memory.brake", "hold": "ArrowUp"}
{"when": "see.memory.brake", "hold": "ArrowDown"}
{"when": "see.memory.steer === 'L'", "hold": "ArrowLeft"}
{"when": "see.memory.steer === 'R'", "hold": "ArrowRight"}
{"when": "see.t - (see.memory.t0 || see.t) > 5000 && (see.locate([22, 22, 22], {from: 'screen', in: [300, 100, 450, 200], tol: 40}) || {n: 0}).n > 12000", "notify": "results screen"}
*/
