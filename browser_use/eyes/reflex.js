// Reflexes: the agent's rules, evaluated every animation frame against what the page draws.
//
// A rule is a condition (a JavaScript expression over `see`) and what to do on its edges: hold a key while it is
// true, tap a key or click when it becomes true, or notify the agent. The page only decides; the input itself goes
// out from Python over CDP, so it arrives as real (trusted) input. Decisions take well under a millisecond: pixels
// are read from a canvas downscaled once per frame, and every `see` reading is memoised for the frame.
//
// `see` (all positions in viewport CSS px):
//   see.locate([r,g,b], {tol, canvas})  centroid {x, y, n} of the pixels near that colour on the largest canvas
//   see.el(selector)                     {x, y, w, h, cx, cy} of an element, or null
//   see.text(selector)                   its visible text
//   see.t, see.frame                     the frame's time (ms) and number
//   see.memory                           an object the rules may keep state in
(() => {
	const RULES = __RULES__;
	const BINDING = '__BINDING__';
	if (window.__reflex) window.__reflex.stop();

	const send = (msg) => {
		try {
			window[BINDING](JSON.stringify(msg));
		} catch (e) {}
	};
	const memo = new Map();
	const off = document.createElement('canvas');
	const offCtx = off.getContext('2d', { willReadFrequently: true });
	const SAMPLE_WIDTH = 256;

	const largestCanvas = () => {
		let best = null;
		let area = 0;
		for (const c of document.querySelectorAll('canvas')) {
			const r = c.getBoundingClientRect();
			if (r.width * r.height > area && c.width && c.height) {
				best = c;
				area = r.width * r.height;
			}
		}
		return best;
	};

	// The canvas, downscaled once this frame: [data, W, H, rect]
	const pixelsOf = (canvas, which) => {
		const key = 'px' + which;
		if (memo.has(key)) return memo.get(key);
		let out = null;
		if (canvas) {
			const W = Math.min(SAMPLE_WIDTH, canvas.width);
			const H = Math.max(1, Math.round((canvas.height * W) / canvas.width));
			if (off.width !== W || off.height !== H) {
				off.width = W;
				off.height = H;
			}
			try {
				offCtx.drawImage(canvas, 0, 0, W, H);
				out = [offCtx.getImageData(0, 0, W, H).data, W, H, canvas.getBoundingClientRect()];
			} catch (e) {
				out = null; // a canvas tainted by cross-origin images cannot be read here
			}
		}
		memo.set(key, out);
		return out;
	};

	const see = {
		t: 0,
		frame: 0,
		memory: {},
		locate(rgb, opts = {}) {
			const key = 'loc' + JSON.stringify([rgb, opts]);
			if (memo.has(key)) return memo.get(key);
			const canvas = opts.canvas ? document.querySelector(opts.canvas) : largestCanvas();
			const px = pixelsOf(canvas, opts.canvas || '*');
			let found = null;
			if (px) {
				const [data, W, H, rect] = px;
				const tol = opts.tol ?? 60;
				let sx = 0;
				let sy = 0;
				let n = 0;
				for (let y = 0; y < H; y++) {
					for (let x = 0; x < W; x++) {
						const i = (y * W + x) * 4;
						if (Math.abs(data[i] - rgb[0]) + Math.abs(data[i + 1] - rgb[1]) + Math.abs(data[i + 2] - rgb[2]) <= tol) {
							sx += x;
							sy += y;
							n++;
						}
					}
				}
				if (n) found = { x: rect.left + ((sx / n + 0.5) / W) * rect.width, y: rect.top + ((sy / n + 0.5) / H) * rect.height, n };
			}
			memo.set(key, found);
			return found;
		},
		el(selector) {
			const key = 'el' + selector;
			if (memo.has(key)) return memo.get(key);
			const e = document.querySelector(selector);
			let out = null;
			if (e) {
				const r = e.getBoundingClientRect();
				out = { x: r.left, y: r.top, w: r.width, h: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
			}
			memo.set(key, out);
			return out;
		},
		text(selector) {
			const key = 'tx' + selector;
			if (memo.has(key)) return memo.get(key);
			const e = document.querySelector(selector);
			const out = e ? (e.innerText || e.textContent || '').trim() : '';
			memo.set(key, out);
			return out;
		},
	};

	const rules = RULES.map((r, i) => ({ ...r, i, fn: new Function('see', '"use strict"; return (' + r.when + ');'), last: false, lastFire: -1e9, errors: 0 }));
	const pending = new Map(); // key -> decision time, to measure when the input arrives
	let running = true;

	const fire = (rule, action, extra = {}) => {
		const msg = { r: rule.i, a: action, wt: performance.now(), ...extra };
		if (msg.key && (action === 'down' || action === 'tap')) pending.set(msg.key, msg.wt);
		send(msg);
	};

	const tick = (now) => {
		if (!running) return;
		see.t = now;
		see.frame++;
		memo.clear();
		for (const rule of rules) {
			let on = false;
			try {
				on = !!rule.fn(see);
			} catch (e) {
				if (rule.errors++ < 3) send({ r: rule.i, a: 'error', error: String((e && e.message) || e) });
			}
			if (on === rule.last) continue;
			rule.last = on;
			if (rule.hold) {
				fire(rule, on ? 'down' : 'up', { key: rule.hold });
				continue;
			}
			if (!on || now - rule.lastFire < (rule.cooldown_ms || 0)) continue;
			rule.lastFire = now;
			if (rule.key) fire(rule, 'tap', { key: rule.key });
			if (rule.click) {
				let at = null;
				try {
					at = new Function('see', '"use strict"; return (' + rule.click + ');')(see);
				} catch (e) {}
				if (at) fire(rule, 'click', { x: at[0], y: at[1] });
			}
			if (rule.notify) fire(rule, 'notify', { label: rule.notify });
		}
		requestAnimationFrame(tick);
	};

	// The input arriving back at the page closes the loop: decision to delivery, on the page's own clock.
	const arrived = (e) => {
		if (!e.isTrusted || !pending.has(e.key)) return;
		send({ a: 'lat', key: e.key, ms: performance.now() - pending.get(e.key) });
		pending.delete(e.key);
	};
	addEventListener('keydown', arrived, true);

	window.__reflex = {
		stop() {
			running = false;
			removeEventListener('keydown', arrived, true);
			return rules.filter((r) => r.hold && r.last).map((r) => r.hold); // keys still held, for release
		},
	};
	requestAnimationFrame(tick);
	return rules.length;
})();
