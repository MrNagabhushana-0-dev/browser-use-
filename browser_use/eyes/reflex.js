// Reflexes: the agent's rules, evaluated every animation frame against what the page draws.
//
// A rule is a condition (a JavaScript expression over `see`) and what to do on its edges: hold a key while it is
// true, tap a key or click when it becomes true, or notify the agent. The page only decides; the input itself goes
// out from Python over CDP, so it arrives as real (trusted) input. Decisions take well under a millisecond: pixels
// are read from a canvas downscaled once per frame, and every `see` reading is memoised for the frame.
//
// `see` (all positions in viewport CSS px):
//   see.locate([r,g,b], {tol, from, in}) centroid {x, y, n} of the pixels near that colour; from: 'canvas' (the
//                                        default, else the largest video), 'video', 'screen' (everything on screen as
//                                        the compositor draws it: cross-origin frames too), or a CSS selector;
//                                        in: [x, y, w, h] looks only there (viewport CSS px), e.g. just ahead of a runner.
//                                        A source the page may not read (a cross-origin video without CORS, a tainted
//                                        canvas) is read from the screen instead, within its box on screen
//   see.changed({from, in})              share (0-1) of the picture (or of that region) changed since the last frame
//   see.track([r,g,b], {tol, from, in})  locate() plus its velocity {vx, vy} in viewport px per second, over the last
//                                        few frames it was seen: where a ball will be, not only where it is
//   see.level(), see.onset(jumpDb)       loudness (dB) of the playing media; true on the frame a sound starts
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
	// The screen as the compositor draws it, fed from Python (__reflex.feed) ~20 times a second once a reading asks
	// for it: {data (RGBA), W, H, seq, t (page time the compositor drew it)}. The page cannot read a cross-origin video or frame
	// itself; the compositor's picture of it is what a person sees.
	let screen = null;
	let screenAsked = false;
	const tracks = new Map(); // per tracked colour and options: the last few sightings [t, x, y], for track()
	const previous = new Map(); // the last frame's grid per source, for changed()
	const shares = new Map(); // per screen reading: [the picture's seq, its share changed], for changed()
	const loudness = []; // recent frames' dB, for onset()
	let ear = null; // {media, ctx, analyser, buf}

	// The playing media's sound through an analyser on a copy of its stream (captureStream leaves the page's own
	// audio as it is). Silent for a cross-origin source without CORS: the page cannot read that sound either.
	const hear = () => {
		const media = [...document.querySelectorAll('video, audio')].find((m) => !m.paused && !m.ended) || null;
		if (!media) return null;
		try {
			if (!ear || ear.media !== media) {
				const ctx = ear ? ear.ctx : new AudioContext();
				const stream = media.captureStream ? media.captureStream() : media.mozCaptureStream();
				if (!stream.getAudioTracks().length) return null;
				const node = ctx.createMediaStreamSource(stream);
				const analyser = ctx.createAnalyser();
				analyser.fftSize = 1024;
				node.connect(analyser);
				ear = { media, ctx, analyser, buf: new Float32Array(analyser.fftSize) };
			}
			if (ear.ctx.state === 'suspended') ear.ctx.resume();
			ear.analyser.getFloatTimeDomainData(ear.buf);
			let sum = 0;
			for (const v of ear.buf) sum += v * v;
			const db = 10 * Math.log10(sum / ear.buf.length + 1e-12);
			loudness.push(db);
			if (loudness.length > 60) loudness.shift();
			return db;
		} catch (e) {
			return null;
		}
	};
	let off = document.createElement('canvas');
	let offCtx = off.getContext('2d', { willReadFrequently: true });
	// Sources the page may not read (getImageData threw once): read on screen from then on. Drawing one taints the
	// scratch canvas for good, so it is replaced, or every later reading of a readable canvas would fail too.
	const unreadable = new WeakSet();
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

	// Each video's frames as they went to the screen, from requestVideoFrameCallback: [presentation time (page ms),
	// media time (s)]. The video's clock runs ~20-30 ms ahead of the frame on screen, and a screen picture is older
	// still, so an event is placed at the frame that was on screen when what the rule saw was drawn. (Keyed by the
	// expected display time instead, a picture of the cut's first frame was drawn just before it, and the cut was
	// placed a frame early in most runs.)
	const frameLogs = new WeakMap();
	const frameLog = (v) => {
		if (!v || !v.requestVideoFrameCallback) return null;
		let log = frameLogs.get(v);
		if (!log) {
			log = [];
			frameLogs.set(v, log);
			const shown = (now, m) => {
				log.push([m.presentationTime, m.mediaTime]);
				if (log.length > 120) log.shift();
				if (running) v.requestVideoFrameCallback(shown);
			};
			v.requestVideoFrameCallback(shown);
		}
		return log;
	};
	const mediaAt = (v, pageMs) => {
		const log = frameLog(v);
		for (let i = log ? log.length - 1 : -1; i >= 0; i--) if (log[i][0] <= pageMs) return log[i][1];
		return null;
	};
	let screenSeen = null; // the page time of the screen picture this frame's readings looked at, if any

	const largestVideo = () => {
		let best = null;
		let score = 0;
		for (const v of document.querySelectorAll('video')) {
			const r = v.getBoundingClientRect();
			const s = r.width * r.height * (v.paused ? 1 : 2);
			if (s > score && v.videoWidth) {
				best = v;
				score = s;
			}
		}
		return best;
	};
	// What a reading looks at: {from: 'canvas'} (the default: the largest canvas, else the largest video), 'video',
	// 'screen', or a CSS selector for a canvas, video or image.
	const sourceOf = (opts) => {
		const from = opts.from || opts.canvas || 'canvas';
		if (from === 'screen') return 'screen';
		if (from === 'video') return largestVideo();
		if (from === 'canvas') return largestCanvas() || largestVideo();
		return document.querySelector(from);
	};
	const sizeOf = (el) => (el instanceof HTMLVideoElement ? [el.videoWidth, el.videoHeight] : el instanceof HTMLImageElement ? [el.naturalWidth, el.naturalHeight] : [el.width, el.height]);

	// The screen's latest picture, the whole viewport, or within `clip` (a viewport box) for one element on it
	const screenPixels = (clip) => {
		if (!screenAsked) {
			screenAsked = true;
			send({ a: 'screen' });
		}
		if (!screen) return null;
		screenSeen = screen.t;
		const [width, height] = screen.span || [innerWidth, innerHeight];
		return { data: screen.data, W: screen.W, H: screen.H, rect: { left: 0, top: 0, width, height }, clip, seq: screen.seq, t: screen.t };
	};

	// The source, downscaled once this frame: {data, W, H, rect, clip, seq, t}. seq and t are the screen picture's
	// when it came from the screen (null for a source read here this frame).
	const pixelsOf = (canvas, which) => {
		const key = 'px' + which;
		if (memo.has(key)) return memo.get(key);
		let out = null;
		if (canvas === 'screen') {
			out = screenPixels(null);
			memo.set(key, out);
			return out;
		}
		if (canvas && unreadable.has(canvas)) {
			const r = canvas.getBoundingClientRect();
			out = screenPixels([r.left, r.top, r.width, r.height]);
			memo.set(key, out);
			return out;
		}
		// A video with no current frame draws nothing, without complaint: transparent black, not its picture
		const [sw, sh] = canvas && !(canvas instanceof HTMLVideoElement && canvas.readyState < 2) ? sizeOf(canvas) : [0, 0];
		if (canvas && sw && sh) {
			const W = Math.min(SAMPLE_WIDTH, sw);
			const H = Math.max(1, Math.round((sh * W) / sw));
			if (off.width !== W || off.height !== H) {
				off.width = W;
				off.height = H;
			}
			try {
				offCtx.drawImage(canvas, 0, 0, W, H);
				out = { data: offCtx.getImageData(0, 0, W, H).data, W, H, rect: canvas.getBoundingClientRect(), clip: null, seq: null, t: see.t };
			} catch (e) {
				// tainted (a cross-origin video without CORS, a canvas drawn from one): read it on screen instead
				unreadable.add(canvas);
				off = document.createElement('canvas');
				offCtx = off.getContext('2d', { willReadFrequently: true });
				const r = canvas.getBoundingClientRect();
				out = screenPixels([r.left, r.top, r.width, r.height]);
			}
		}
		memo.set(key, out);
		return out;
	};

	// The part of a sampled source a reading covers, in sample px: all of it, or the viewport region opts.in, kept
	// within clip (the element's box, when it is read on screen).
	const boxOf = (opts, W, H, rect, clip) => {
		let region = opts.in || clip;
		if (!region) return [0, 0, W, H];
		if (opts.in && clip) {
			const x = Math.max(opts.in[0], clip[0]);
			const y = Math.max(opts.in[1], clip[1]);
			region = [x, y, Math.max(0, Math.min(opts.in[0] + opts.in[2], clip[0] + clip[2]) - x), Math.max(0, Math.min(opts.in[1] + opts.in[3], clip[1] + clip[3]) - y)];
		}
		const [x, y, w, h] = region;
		const sx = (v) => Math.max(0, Math.min(W, Math.floor(((v - rect.left) / rect.width) * W)));
		const sy = (v) => Math.max(0, Math.min(H, Math.floor(((v - rect.top) / rect.height) * H)));
		return [sx(x), sy(y), Math.max(sx(x) + 1, sx(x + w)), Math.max(sy(y) + 1, sy(y + h))];
	};

	const see = {
		t: 0,
		frame: 0,
		memory: {},
		locate(rgb, opts = {}) {
			const key = 'loc' + JSON.stringify([rgb, opts]);
			if (memo.has(key)) return memo.get(key);
			const px = pixelsOf(sourceOf(opts), opts.from || opts.canvas || 'canvas');
			let found = null;
			if (px) {
				const { data, W, H, rect, clip } = px;
				const tol = opts.tol ?? 60;
				const [x0, y0, x1, y1] = boxOf(opts, W, H, rect, clip);
				let sx = 0;
				let sy = 0;
				let n = 0;
				for (let y = y0; y < Math.min(H, y1); y++) {
					for (let x = x0; x < Math.min(W, x1); x++) {
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
		// Where a colour is and how fast it moves (px per second), from its last few sightings. Not seen this frame:
		// null, and its history starts over.
		track(rgb, opts = {}) {
			const key = 'tr' + JSON.stringify([rgb, opts]);
			if (memo.has(key)) return memo.get(key);
			const at = see.locate(rgb, opts);
			const px = pixelsOf(sourceOf(opts), opts.from || opts.canvas || 'canvas');
			const t = px && px.seq !== null ? px.t : see.t; // a screen picture is as old as it is, not this frame
			let out = null;
			if (!at) {
				tracks.delete(key);
			} else {
				const seen = tracks.get(key) || [];
				if (!seen.length || seen[seen.length - 1][0] !== t) seen.push([t, at.x, at.y]);
				while (seen.length > 6) seen.shift();
				tracks.set(key, seen);
				const [t0, x0, y0] = seen[0];
				const dt = (t - t0) / 1000;
				out = { ...at, vx: dt > 0 ? (at.x - x0) / dt : 0, vy: dt > 0 ? (at.y - y0) / dt : 0 };
			}
			memo.set(key, out);
			return out;
		},
		// The share (0-1) of the picture that changed since the last frame: motion, a cut, a flash.
		changed(opts = {}) {
			const which = opts.from || opts.canvas || 'canvas';
			const key = 'ch' + which + JSON.stringify(opts.in || null);
			if (memo.has(key)) return memo.get(key);
			const px = pixelsOf(sourceOf(opts), which);
			let share = 0;
			if (px && px.seq !== null && shares.has(key) && shares.get(key)[0] === px.seq) {
				share = shares.get(key)[1]; // the screen has not been drawn again since: the same answer as last frame
			} else if (px) {
				const { data, W, H, rect, clip } = px;
				const [x0, y0, x1, y1] = boxOf(opts, W, H, rect, clip);
				const grid = new Uint8Array(32 * 18);
				for (let gy = 0; gy < 18; gy++) {
					for (let gx = 0; gx < 32; gx++) {
						const sy = Math.min(H - 1, y0 + Math.floor(((gy + 0.5) * (y1 - y0)) / 18));
						const sx = Math.min(W - 1, x0 + Math.floor(((gx + 0.5) * (x1 - x0)) / 32));
						const i = (sy * W + sx) * 4;
						grid[gy * 32 + gx] = (data[i] * 299 + data[i + 1] * 587 + data[i + 2] * 114) / 1000;
					}
				}
				const prev = previous.get(key);
				if (prev) {
					let n = 0;
					for (let k = 0; k < grid.length; k++) if (Math.abs(grid[k] - prev[k]) > 24) n++;
					share = n / grid.length;
				}
				previous.set(key, grid);
				if (px.seq !== null) shares.set(key, [px.seq, share]);
			}
			memo.set(key, share);
			return share;
		},
		// Loudness (dB) of the playing media's sound right now, or null when there is none to hear.
		level() {
			if (memo.has('lv')) return memo.get('lv');
			const db = hear();
			memo.set('lv', db);
			return db;
		},
		// True on the frame a sound starts: at least `jump` dB louder than the frames just before.
		onset(jump = 12) {
			const db = see.level();
			if (db === null || loudness.length < 6) return false;
			const before = loudness.slice(-12, -1).sort((a, b) => a - b)[Math.floor(Math.min(11, loudness.length - 1) / 2)];
			return db > -50 && db - before >= jump;
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
		const media = largestVideo();
		const seenAt = screenSeen !== null ? screenSeen : performance.now();
		const shown = media ? mediaAt(media, seenAt) : null;
		const msg = { r: rule.i, a: action, wt: performance.now(), mt: media ? (shown !== null ? shown : media.currentTime) : null, ...extra };
		if (msg.key && (action === 'down' || action === 'tap')) pending.set(msg.key, msg.wt);
		send(msg);
	};

	const tick = (now) => {
		if (!running) return;
		see.t = now;
		see.frame++;
		memo.clear();
		screenSeen = null;
		frameLog(largestVideo()); // frames are logged from before a rule needs them
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
		// Python's picture of the screen: W x H, RGB, base64, drawn at `drawn` (seconds since the epoch). Returns that on the page's clock.
		feed(W, H, b64, seq, drawn, span) {
			const bin = atob(b64);
			const n = W * H;
			const data = new Uint8ClampedArray(n * 4);
			for (let p = 0, q = 0; p < n; p++, q += 3) {
				data[p * 4] = bin.charCodeAt(q);
				data[p * 4 + 1] = bin.charCodeAt(q + 1);
				data[p * 4 + 2] = bin.charCodeAt(q + 2);
				data[p * 4 + 3] = 255;
			}
			// t: when the compositor drew it (drawn: seconds since the epoch), on this page's clock
			// span: the CSS px the picture covers (a screencast frame takes in the scrollbar, a capture does not)
			screen = { data, W, H, seq, span, t: drawn ? drawn * 1000 - performance.timeOrigin : performance.now() };
			return screen.t;
		},
		stop() {
			running = false;
			removeEventListener('keydown', arrived, true);
			return rules.filter((r) => r.hold && r.last).map((r) => r.hold); // keys still held, for release
		},
	};
	requestAnimationFrame(tick);
	return rules.length;
})();
