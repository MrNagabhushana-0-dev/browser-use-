// MR Racer rule v3 (2026-10-20): dodge only into a lane whose boxes read clear, otherwise brake. Score 163.
// retinat_reflex install, screen_width 480; rule 0 decides, the others hold keys:
(() => {
  const m = see.memory;
  m.t0 ??= see.t;
  const t = see.t - m.t0;
  const dark = (box) => { const a = see.locate([30, 25, 25], {from: 'screen', in: box, tol: 150}); return a ? a.n : 0; };
  if (t > 9000 && t < 9300) { m.steer = 'R'; m.lane = 0; return false; }
  if (t < 9300) { m.steer = null; return false; }
  m.ahead = dark([500, 110, 60, 28]);
  const rClear = dark([556, 110, 56, 28]) < 8 && dark([590, 140, 110, 70]) < 40;
  const lClear = dark([448, 110, 56, 28]) < 8 && dark([360, 140, 110, 70]) < 150;
  if (see.t < (m.until || 0)) return false;
  m.steer = null;
  m.brake = false;
  if (m.ahead > 15) {
    if (see.t >= (m.calm || 0)) {
      const goR = m.lane < 1 && rClear;
      const goL = m.lane > -2 && lClear;
      if (goR || goL) {
        m.steer = goR ? 'R' : 'L';
        m.lane += goR ? 1 : -1;
        m.until = see.t + 300;
        m.calm = see.t + 1500;
        m.moves = (m.moves || 0) + 1;
        return false;
      }
    }
    m.brake = true;
  }
  return false;
})()
/* key rules:
{"when": "!see.memory.brake", "hold": "ArrowUp"}
{"when": "see.memory.brake", "hold": "ArrowDown"}
{"when": "see.memory.steer === 'L'", "hold": "ArrowLeft", "notify": "moved left"}
{"when": "see.memory.steer === 'R'", "hold": "ArrowRight"}
{"when": "see.memory.brake", "notify": "braking"}
{"when": "see.t - (see.memory.t0 || see.t) > 5000 && (see.locate([22, 22, 22], {from: 'screen', in: [300, 100, 450, 200], tol: 40}) || {n: 0}).n > 12000", "notify": "results screen"}
*/
