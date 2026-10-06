// The retina: runs inside the page, in an isolated world the page's own scripts cannot see.
//
// It does three things and nothing else:
//   1. Attention. Picks the one video a person would be looking at: the largest one on screen,
//      preferring one that is playing. Reports when that changes (a swipe to the next reel).
//   2. Sight. Taps that video's decoded frames with requestVideoFrameCallback, which fires once
//      per frame the compositor actually presents, and reduces each sampled frame to a 16x16
//      luma grid (256 bytes). It also keeps a small ring of JPEG keyframes, taken whenever the
//      picture has changed or half a second has passed, which Python asks for by number. No
//      screenshot is ever taken: the pixels come from the video element itself.
//   3. Hearing. Taps the same video's audio with captureStream() into an AudioWorklet that
//      computes, every 1024 samples (~22 ms), loudness, zero-crossing rate, spectral centroid,
//      flux and flatness, and a 24-band log spectrum. Raw audio never leaves the page.
//
// Everything is pushed to Python through a CDP binding in batches, about five times a second.
// Python owns all interpretation; this file only measures.
(() => {
	const VERSION = 9;
	if (window.__retina && window.__retina.version === VERSION) return;
	if (window.__retina) window.__retina.stop();

	const EMIT = '__retina_emit';
	const DEFAULTS = {
		fps: 10, // luma-grid samples per second of media time
		thumbEvery: 0.5, // seconds of media time between routine keyframes
		thumbDiff: 18, // luma-grid distance (0-255 mean abs) that forces a keyframe
		thumbWidth: 320, // keyframe width, CSS pixels
		thumbMax: 240, // keyframes kept in the ring
		audio: true,
		pcm: false, // also send 16 kHz PCM, for speech detection and transcription
		listen: false, // unmute the attended video so it can be heard
		flushMs: 200,
		attendMs: 250,
		minArea: 0.08, // fraction of the viewport a video must cover to be attended
	};
	const opts = Object.assign({}, DEFAULTS, window.__retinaOpts || {});
	const GRID = 16;
	const BANDS = 24;

	const R = {
		version: VERSION,
		opts,
		running: false,
		ids: new WeakMap(),
		nextId: 1,
		// Unique across documents, so a reload never reuses the previous page's item numbers.
		nextItem: (Math.floor(performance.timeOrigin) % 1e7) * 1000 + 1,
		attended: null,
		attendedSrc: '',
		items: new Map(),
		held: null,
		attendedId: 0,
		seq: 0,
		frames: [],
		audio: [],
		events: [],
		thumbs: new Map(),
		lastThumbGrid: null,
		lastThumbT: -1,
		lastSampleT: -1,
		tainted: new WeakSet(),
		ctx: null,
		node: null,
		audioMode: 'off',
		audioSource: null,
		timers: [],
	};

	const emit = (obj) => {
		try {
			window[EMIT](JSON.stringify(obj));
			return true;
		} catch (e) {
			return false;
		}
	};
	const idOf = (v) => {
		let id = R.ids.get(v);
		if (!id) {
			id = R.nextId++;
			R.ids.set(v, id);
		}
		return id;
	};
	const b64 = (bytes) => {
		let s = '';
		for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
		return btoa(s);
	};

	// -- attention -----------------------------------------------------------------------

	const visible = (el) => {
		const r = el.getBoundingClientRect();
		const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
		const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
		return { area: w * h, rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)] };
	};

	const pick = () => {
		const vp = innerWidth * innerHeight;
		let best = null;
		let bestScore = 0;
		for (const v of document.querySelectorAll('video')) {
			const { area } = visible(v);
			if (area < opts.minArea * vp) continue;
			const playing = !v.paused && !v.ended && v.readyState > 2;
			const score = area * (playing ? 2 : 1);
			if (score > bestScore) {
				best = v;
				bestScore = score;
			}
		}
		if (best) return best;
		// No video: a large canvas (2D or WebGL) is where an animation, a game or a chart is drawn.
		for (const c of document.querySelectorAll('canvas')) {
			const { area } = visible(c);
			if (area >= opts.minArea * vp && area > bestScore) {
				best = c;
				bestScore = area;
			}
		}
		return best;
	};
	const isCanvas = (el) => el instanceof HTMLCanvasElement;
	const dims = (el) => (isCanvas(el) ? [el.width, el.height] : [el.videoWidth, el.videoHeight]);

	// Text a person would read next to the video: caption, author, audio credit. Short on
	// purpose: this is what is on screen, not the page's markup.
	const nearbyText = (v) => {
		let el = v;
		for (let i = 0; i < 8 && el; i++) {
			el = el.parentElement;
			if (!el) break;
			const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
			if (t.length >= 12) return t.slice(0, 400);
		}
		return '';
	};

	const describe = (v) => isCanvas(v) ? {
		vid: v === R.attended ? R.attendedId : 0,
		el: idOf(v),
		kind: 'canvas',
		order: -1,
		src: 'canvas',
		w: v.width,
		h: v.height,
		duration: null,
		t: Math.max(0, R.lastSampleT),
		paused: false,
		muted: true,
		rect: visible(v).rect,
		text: nearbyText(v),
	} : ({
		vid: v === R.attended ? R.attendedId : 0,
		kind: 'video',
		el: idOf(v),
		// Position among the page's videos in document order: how a feed says which item is next.
		order: Array.prototype.indexOf.call(document.querySelectorAll('video'), v),
		src: String(v.currentSrc || v.src || '').slice(0, 160),
		w: v.videoWidth,
		h: v.videoHeight,
		duration: isFinite(v.duration) ? v.duration : null,
		t: v.currentTime,
		paused: v.paused,
		muted: v.muted || v.volume === 0,
		rect: visible(v).rect,
		text: nearbyText(v),
	});

	// The src attribute first: setting it resets currentTime at once, while currentSrc keeps the old URL until
	// resource selection runs, so a hop or frame in between would carry the old item's id at the new time 0.
	const srcOf = (v) => String(v.src || v.currentSrc || '');

	// An *item* is one thing watched: a new element, or the same element given a new source
	// (virtualized feeds recycle a few <video> elements for every reel).
	const attend = () => {
		const v = pick();
		const src = v ? (isCanvas(v) ? 'canvas' : srcOf(v)) : '';
		if (v === R.attended && src === R.attendedSrc) return;
		R.attended = v;
		R.attendedSrc = src;
		// Coming back to something already watched (scrolling up, correcting an overshoot) is
		// the same item again, not a new one.
		const key = v ? idOf(v) + '|' + src : '';
		if (v && !R.items.has(key)) R.items.set(key, R.nextItem++);
		R.attendedId = v ? R.items.get(key) : 0;
		R.lastSampleT = -1;
		R.lastThumbGrid = null;
		R.lastThumbT = -1;
		R.events.push(Object.assign({ type: 'attend', wt: performance.now() }, v ? describe(v) : { vid: 0 }));
		if (v && isCanvas(v)) {
			stopHearing(); // a canvas has no sound of its own
			startCanvasSight(v);
		} else if (v) {
			if (opts.listen && v.muted) v.muted = false;
			startSight(v);
			if (opts.audio) startHearing(v);
		} else {
			stopHearing();
		}
	};

	// -- sight ---------------------------------------------------------------------------

	const gridCanvas = new OffscreenCanvas(GRID, GRID);
	const grid2d = gridCanvas.getContext('2d', { willReadFrequently: true });

	const sampleGrid = (v) => {
		// A canvas is area-averaged when shrunk to 16x16: the default point-samples, so a small drawn object
		// aliases (flickers between cells) and its motion reads as jitter. Video sampling stays as it was.
		grid2d.imageSmoothingQuality = isCanvas(v) ? 'high' : 'low';
		grid2d.drawImage(v, 0, 0, GRID, GRID);
		const d = grid2d.getImageData(0, 0, GRID, GRID).data;
		const luma = new Uint8Array(GRID * GRID);
		let r = 0,
			g = 0,
			b = 0;
		for (let i = 0, p = 0; i < luma.length; i++, p += 4) {
			luma[i] = (0.299 * d[p] + 0.587 * d[p + 1] + 0.114 * d[p + 2]) | 0;
			r += d[p];
			g += d[p + 1];
			b += d[p + 2];
		}
		const n = luma.length;
		// A 4x4 colour grid (48 bytes): enough to tell colourful from black-and-white, and which hues.
		const c4 = new Uint8Array(48);
		for (let cy = 0; cy < 4; cy++)
			for (let cx = 0; cx < 4; cx++) {
				let sr = 0, sg = 0, sb = 0;
				for (let y = cy * 4; y < cy * 4 + 4; y++)
					for (let x = cx * 4; x < cx * 4 + 4; x++) {
						const p = (y * GRID + x) * 4;
						sr += d[p]; sg += d[p + 1]; sb += d[p + 2];
					}
				const k = (cy * 4 + cx) * 3;
				c4[k] = sr >> 4; c4[k + 1] = sg >> 4; c4[k + 2] = sb >> 4;
			}
		return { luma, rgb: [(r / n) | 0, (g / n) | 0, (b / n) | 0], c4 };
	};

	const gridDistance = (a, b) => {
		let s = 0;
		for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]);
		return s / a.length;
	};

	const captureThumb = (v, seq) => {
		const [vw, vh] = dims(v);
		if (!vw) return;
		const w = Math.min(opts.thumbWidth, vw);
		const h = Math.max(1, Math.round((w * vh) / vw));
		const c = new OffscreenCanvas(w, h);
		c.getContext('2d').drawImage(v, 0, 0, w, h);
		c.convertToBlob({ type: 'image/jpeg', quality: 0.72 }).then((blob) => {
			R.thumbs.set(seq, blob);
			while (R.thumbs.size > opts.thumbMax) R.thumbs.delete(R.thumbs.keys().next().value);
		});
	};

	const startSight = (v) => {
		if (v.__retinaTapped) return;
		v.__retinaTapped = true;
		const onFrame = (now, meta) => {
			if (!R.running || R.attended !== v) {
				v.__retinaTapped = false;
				return;
			}
			v.requestVideoFrameCallback(onFrame);
			// A new source on this element is a new item from its first frame, not from the next attend poll:
			// stamped with the old id, its restart at 0 reads as a rewind of the old item.
			if (srcOf(v) !== R.attendedSrc) {
				attend();
				if (R.attended !== v) return;
			}
			const mt = meta.mediaTime;
			// A backwards jump is a loop or a seek: always sample it.
			if (mt >= R.lastSampleT && mt - R.lastSampleT < 1 / opts.fps) return;
			R.lastSampleT = mt;
			sampleFrame(v, mt, now);
		};
		v.requestVideoFrameCallback(onFrame);
	};

	// A canvas has no frame callback or media clock: sample it once per animation frame, after the
	// page has drawn (2D, or WebGL with preserveDrawingBuffer), on a clock that starts when attended.
	const startCanvasSight = (c) => {
		if (c.__retinaTapped) return;
		c.__retinaTapped = true;
		const t0 = performance.now();
		const onFrame = (now) => {
			if (!R.running || R.attended !== c) {
				c.__retinaTapped = false;
				return;
			}
			requestAnimationFrame(onFrame);
			const mt = (now - t0) / 1000;
			if (mt - R.lastSampleT < 1 / opts.fps) return;
			R.lastSampleT = mt;
			sampleFrame(c, mt, now);
		};
		requestAnimationFrame(onFrame);
	};

	const sampleFrame = (v, mt, now) => {
		{
			if (R.tainted.has(v)) return;
			let s;
			try {
				s = sampleGrid(v);
			} catch (e) {
				R.tainted.add(v);
				R.events.push({ type: 'tainted', vid: R.attendedId, wt: performance.now(), reason: String(e && e.name) });
				return;
			}
			const seq = ++R.seq;
			let thumb = false;
			if (
				!R.lastThumbGrid ||
				mt < R.lastThumbT ||
				mt - R.lastThumbT >= opts.thumbEvery ||
				gridDistance(s.luma, R.lastThumbGrid) >= opts.thumbDiff
			) {
				thumb = true;
				R.lastThumbGrid = s.luma;
				R.lastThumbT = mt;
				captureThumb(v, seq);
			}
			R.frames.push([seq, R.attendedId, +mt.toFixed(3), +now.toFixed(1), b64(s.luma), s.rgb, thumb ? 1 : 0, b64(s.c4)]);
		}
	};

	// -- hearing -------------------------------------------------------------------------

	// Runs on the audio thread. Mono mixdown, 1024-sample hops, Hann window, radix-2 FFT.
	const WORKLET = `
const N = 1024, BANDS = ${BANDS};
class RetinaEar extends AudioWorkletProcessor {
	constructor(options) {
		super();
		// Optional 16 kHz mono PCM for a speech model downstream. Box-filtered then sampled
		// at the target rate: crude, but speech energy is below 4 kHz, far under the 8 kHz Nyquist.
		this.pcm = !!(options && options.processorOptions && options.processorOptions.pcm);
		this.step = sampleRate / 16000; this.phase = 0; this.acc = 0; this.accN = 0;
		this.out = new Int16Array(512); this.outN = 0;
		this.buf = new Float32Array(N); this.n = 0;
		this.re = new Float32Array(N); this.im = new Float32Array(N);
		this.prev = new Float32Array(N / 2);
		this.win = new Float32Array(N);
		for (let i = 0; i < N; i++) this.win[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (N - 1));
		const lo = 50, hi = Math.min(11000, sampleRate / 2 - 1);
		this.edges = [];
		for (let b = 0; b <= BANDS; b++) this.edges.push(Math.round((lo * Math.pow(hi / lo, b / BANDS)) * N / sampleRate));
	}
	fft() {
		const re = this.re, im = this.im;
		for (let i = 1, j = 0; i < N; i++) {
			let bit = N >> 1;
			for (; j & bit; bit >>= 1) j ^= bit;
			j ^= bit;
			if (i < j) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
		}
		for (let len = 2; len <= N; len <<= 1) {
			const ang = (-2 * Math.PI) / len, wr = Math.cos(ang), wi = Math.sin(ang);
			for (let i = 0; i < N; i += len) {
				let cr = 1, ci = 0;
				for (let k = 0; k < len / 2; k++) {
					const a = i + k, b = a + len / 2;
					const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
					re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
					const nr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = nr;
				}
			}
		}
	}
	hop() {
		const x = this.buf;
		let e = 0, z = 0;
		for (let i = 0; i < N; i++) { e += x[i] * x[i]; if (i && (x[i] >= 0) !== (x[i - 1] >= 0)) z++; this.re[i] = x[i] * this.win[i]; this.im[i] = 0; }
		const rms = 10 * Math.log10(e / N + 1e-12);
		this.fft();
		let p = 0, pf = 0, flux = 0, magSum = 0, logSum = 0, peak = 0, peakBin = 0;
		const H = N / 2;
		for (let k = 1; k < H; k++) {
			const m = Math.hypot(this.re[k], this.im[k]);
			const pw = m * m;
			p += pw; pf += pw * k; magSum += m; logSum += Math.log(pw + 1e-20);
			const d = m - this.prev[k]; if (d > 0) flux += d;
			this.prev[k] = m;
			if (pw > peak) { peak = pw; peakBin = k; }
		}
		const hz = sampleRate / N;
		const bands = new Uint8Array(BANDS);
		for (let b = 0; b < BANDS; b++) {
			let s = 0; const a = Math.max(1, this.edges[b]), c = Math.max(a + 1, this.edges[b + 1]);
			for (let k = a; k < c && k < H; k++) s += this.re[k] * this.re[k] + this.im[k] * this.im[k];
			const db = 10 * Math.log10(s / (c - a) + 1e-12);
			bands[b] = Math.max(0, Math.min(255, Math.round((db + 40) * 2.55)));
		}
		// Parabolic interpolation on log magnitude: bins are ~43 Hz wide, a voice or a note is not.
		let peakHz = peakBin * hz;
		if (peakBin > 1 && peakBin < H - 1) {
			const m = (k) => Math.log(Math.hypot(this.re[k], this.im[k]) + 1e-20);
			const a = m(peakBin - 1), b = m(peakBin), c = m(peakBin + 1), den = a - 2 * b + c;
			if (den < 0) peakHz = (peakBin + (0.5 * (a - c)) / den) * hz;
		}
		// Undefined for (near) digital silence, where it is a ratio of two vanishing numbers.
		const flat = rms > -90 && p > 0 ? Math.min(1, Math.exp(logSum / (H - 1)) / (p / (H - 1))) : 0;
		let pcm = null;
		if (this.pcm) { pcm = this.out.slice(0, this.outN); this.outN = 0; }
		this.port.postMessage([
			currentTime, +rms.toFixed(1), +(z / N).toFixed(4), p > 0 ? Math.round((pf / p) * hz) : 0,
			magSum > 0 ? +(flux / magSum).toFixed(4) : 0, +flat.toFixed(4), Math.round(peakHz), bands, pcm,
		]);
	}
	process(inputs) {
		const inp = inputs[0];
		if (!inp || !inp.length || !inp[0]) return true;
		const len = inp[0].length, ch = inp.length;
		for (let i = 0; i < len; i++) {
			let s = 0; for (let c = 0; c < ch; c++) s += inp[c][i];
			const x = s / ch;
			this.buf[this.n++] = x;
			if (this.pcm) {
				this.acc += x; this.accN++; this.phase += 1;
				if (this.phase >= this.step) {
					this.phase -= this.step;
					if (this.outN < this.out.length) this.out[this.outN++] = Math.max(-32768, Math.min(32767, Math.round((this.acc / this.accN) * 32767)));
					this.acc = 0; this.accN = 0;
				}
			}
			if (this.n === N) { this.hop(); this.n = 0; }
		}
		return true;
	}
}
registerProcessor('retina-ear', RetinaEar);
`;

	const ensureEar = async () => {
		if (R.node) return R.node;
		if (!R.ctx) R.ctx = new AudioContext({ latencyHint: 'playback' });
		if (R.ctx.state !== 'running') {
			try {
				await R.ctx.resume();
			} catch (e) {}
		}
		const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
		await R.ctx.audioWorklet.addModule(url);
		URL.revokeObjectURL(url);
		const node = new AudioWorkletNode(R.ctx, 'retina-ear', { numberOfOutputs: 0, processorOptions: { pcm: !!opts.pcm } });
		node.port.onmessage = (m) => {
			const v = R.attended;
			if (!v || !R.running) return;
			if (!isCanvas(v) && srcOf(v) !== R.attendedSrc) {
				attend(); // the source changed under this hop: whose sound it is, is not known
				return;
			}
			const a = m.data;
			// The worklet stamps each hop with audio-context time. Messages can reach this thread
			// late and in bursts when it is busy; stamping them with the media time *at arrival*
			// would pile a burst onto one instant. Subtract how long the hop waited instead.
			const lag = Math.max(0, R.ctx.currentTime - a[0]);
			a[0] = +Math.max(0, v.currentTime - lag * (v.playbackRate || 1)).toFixed(3);
			a[7] = b64(a[7]);
			a[8] = a[8] ? b64(new Uint8Array(a[8].buffer)) : '';
			a.push(R.attendedId);
			R.audio.push(a);
		};
		R.node = node;
		return node;
	};

	const stopHearing = () => {
		if (R.audioSource) {
			try {
				R.audioSource.disconnect();
			} catch (e) {}
		}
		R.audioSource = null;
		R.audioTrack = null;
	};

	const startHearing = async (v) => {
		stopHearing();
		try {
			const node = await ensureEar();
			if (R.attended !== v) return;
			if (!v.__retinaStream) {
				v.__retinaStream = v.captureStream();
				// A new source on the same element ends the old tracks and adds new ones to this stream:
				// follow onto them, or the ear sits on a dead track and everything after reads as silence.
				v.__retinaStream.addEventListener('addtrack', (e) => {
					if (e.track.kind === 'audio' && R.attended === v) startHearing(v);
				});
			}
			// One track, the newest: given several, createMediaStreamSource takes the one whose id sorts
			// first, which after a source change is as likely the old, silent one.
			const tracks = v.__retinaStream.getAudioTracks().filter((t) => t.readyState === 'live');
			if (!tracks.length) {
				// Media Source players add their audio track after the first segment; retried
				// by the heartbeat.
				R.audioMode = 'no-track';
				return;
			}
			const track = tracks[tracks.length - 1];
			R.audioSource = R.ctx.createMediaStreamSource(new MediaStream([track]));
			R.audioSource.connect(node);
			R.audioTrack = track;
			R.audioMode = 'worklet';
		} catch (e) {
			R.audioMode = 'error: ' + String((e && e.message) || e).slice(0, 120);
		}
	};

	// -- transient text ------------------------------------------------------------------
	// Words that appear on the page: toasts, banners, alerts, a live value changing. A screenshot or
	// a DOM snapshot only sees them if it happens to be taken while they are up; this sees them arrive.
	let textObserver = null;
	let textBudget = { second: 0, n: 0 };
	const lastText = new Map(); // text -> last time reported, to skip repeats within a second
	const isShown = (el) => {
		const style = getComputedStyle(el);
		if (style.display === 'none' || style.visibility === 'hidden' || +style.opacity === 0) return false;
		const rect = el.getBoundingClientRect();
		return rect.width >= 2 && rect.height >= 2;
	};
	// Pages often build a toast or a carousel slide hidden and reveal it with a class or style change: no text is
	// inserted, so only the attribute change says it appeared. Visibility last seen per element, to tell a reveal
	// from a restyle of something already on screen.
	const shownBefore = new WeakMap();
	const REVEALING = ['class', 'style', 'hidden', 'aria-hidden', 'open'];
	const revealed = (m) => {
		const el = m.target;
		if (el.nodeType !== 1 || !el.isConnected) return false;
		// The root changing class (a theme, a 'loaded' flag) is the page setting state, not something appearing.
		if (el === document.documentElement || el === document.body) return false;
		const now = isShown(el);
		const before = shownBefore.get(el);
		shownBefore.set(el, now);
		if (!now || before === true) return false;
		if (before === false) return true;
		// First change seen on this element: judge what it was from the old value where it says; a class change
		// cannot be judged, and is taken as a reveal (at most once per element).
		const old = m.oldValue;
		if (m.attributeName === 'style') return /display:\s*none|visibility:\s*hidden|opacity:\s*0(?![.\d])/.test(old || '');
		if (m.attributeName === 'hidden') return old !== null;
		if (m.attributeName === 'aria-hidden') return old === 'true';
		if (m.attributeName === 'open') return old === null;
		return true;
	};
	// WCAG contrast of an element's text against the nearest opaque background up its ancestors (images and
	// gradients are not seen: they read as the colour behind them). Under ~1.5:1 a person can barely see the text,
	// a technique used to hide instructions meant for AI agents.
	const rgbOf = (c) => (c.match(/[\d.]+/g) || []).map(Number);
	const luminance = ([r, g, b]) => {
		const f = (v) => ((v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
		return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
	};
	const contrast = (el) => {
		const fg = rgbOf(getComputedStyle(el).color);
		let bg = [255, 255, 255];
		for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
			const c = rgbOf(getComputedStyle(n).backgroundColor);
			if (c.length >= 3 && (c.length < 4 || c[3] > 0.5)) {
				bg = c;
				break;
			}
		}
		if (fg.length < 3) return 21;
		const [a, b] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
		return (a + 0.05) / (b + 0.05);
	};
	const reportText = (el) => {
		if (!el || el.nodeType !== 1 || !el.isConnected || el.closest('video, script, style, noscript')) return;
		if (!isShown(el)) return;
		const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
		if (!text || text.length > 240) return;
		const now = performance.now();
		const sec = Math.floor(now / 1000);
		if (textBudget.second !== sec) textBudget = { second: sec, n: 0 };
		if (++textBudget.n > 8) return; // a ticker repainting every frame is not news
		if (now - (lastText.get(text) || -1e9) < 1000) return;
		lastText.set(text, now);
		const faint = contrast(el) < 1.5;
		R.events.push(Object.assign({ type: 'text', wt: now, text, vid: R.attendedId }, faint ? { faint: true } : {}));
	};
	let textReady = false; // false while the page is still being parsed: its own content has not "appeared"
	const watchText = () => {
		if (textObserver) return;
		// Observe the document node itself: it exists from the first instant, before <html> and <body> (the retina
		// autostarts at document start), so text added right after load is not missed. Ignore everything until
		// the initial parse is done: the page's own content has not "appeared".
		textReady = document.readyState !== 'loading';
		if (!textReady) document.addEventListener('DOMContentLoaded', () => (textReady = true), { once: true });
		textObserver = new MutationObserver((mutations) => {
			if (!textReady) return;
			const touched = new Set();
			const restyled = [];
			for (const m of mutations) {
				if (m.type === 'characterData') touched.add(m.target.parentElement);
				else if (m.type === 'attributes') restyled.push(m);
				for (const n of m.addedNodes) touched.add(n.nodeType === 1 ? n : n.parentElement);
			}
			// Read styles just after the mutation settles. Not requestAnimationFrame: it never fires in a hidden or
			// background tab, which is exactly where an agent's page often is.
			setTimeout(() => {
				for (const m of restyled) if (revealed(m)) touched.add(m.target);
				touched.forEach(reportText);
			}, 0);
		});
		textObserver.observe(document, {
			childList: true,
			subtree: true,
			characterData: true,
			attributes: true,
			attributeFilter: REVEALING,
			attributeOldValue: true,
		});
	};

	const heartbeat = () => {
		watchText(); // in case the document element did not exist when the retina started
		const v = R.attended;
		if (R.ctx && R.ctx.state === 'suspended') R.ctx.resume().catch(() => {});
		const media = v && !isCanvas(v) ? v : null;
		const stream = media && media.__retinaStream;
		const gone = R.audioTrack && (R.audioTrack.readyState === 'ended' || (stream && !stream.getAudioTracks().includes(R.audioTrack)));
		const deaf = R.audioMode === 'no-track' || R.audioMode.startsWith('error') || gone;
		if (media && opts.audio && deaf) startHearing(media);
		if (media && opts.listen && media.muted) media.muted = false;
		R.events.push({
			type: 'state',
			wt: performance.now(),
			vid: R.attendedId,
			t: media ? media.currentTime : v ? Math.max(0, R.lastSampleT) : null,
			paused: media ? media.paused : v ? false : null,
			muted: media ? media.muted || media.volume === 0 : v ? true : null,
			audio: R.audioMode,
			ctx: R.ctx ? R.ctx.state : 'none',
			sr: R.ctx ? R.ctx.sampleRate : null,
			// A muted or ended capture track delivers silence: say so rather than let it read as a quiet video.
			track: R.audioTrack ? (R.audioTrack.readyState === 'ended' ? 'ended' : R.audioTrack.muted ? 'muted' : 'live') : null,
			hidden: document.hidden,
			url: location.href.slice(0, 200),
		});
	};

	const flush = () => {
		if (!R.frames.length && !R.audio.length && !R.events.length) return;
		const batch = { type: 'batch', f: R.frames, a: R.audio, e: R.events };
		R.frames = [];
		R.audio = [];
		R.events = [];
		if (!emit(batch)) {
			// No listener attached (yet). Keep the most recent few seconds instead of growing forever.
			R.frames = batch.f.slice(-50);
			R.audio = batch.a.slice(-250);
			R.events = batch.e.slice(-20);
		}
	};

	R.start = (overrides) => {
		Object.assign(opts, overrides || {});
		if (R.running) return R.state();
		watchText();
		R.running = true;
		R.timers.push(setInterval(attend, opts.attendMs));
		R.timers.push(setInterval(flush, opts.flushMs));
		R.timers.push(setInterval(heartbeat, 1000));
		attend();
		heartbeat();
		return R.state();
	};

	R.stop = () => {
		R.running = false;
		for (const t of R.timers) clearInterval(t);
		R.timers = [];
		stopHearing();
		R.attended = null;
		R.attendedId = 0;
	};

	R.state = () => {
		const v = R.attended;
		return {
			version: VERSION,
			running: R.running,
			vid: R.attendedId,
			attended: v ? describe(v) : null,
			audio: R.audioMode,
			ctx: R.ctx ? R.ctx.state : 'none',
			tainted: v ? R.tainted.has(v) : false,
			thumbs: R.thumbs.size,
		};
	};

	// Keyframes by sequence number, as data URLs. Missing ones (evicted) come back null.
	R.keyframes = async (seqs) => {
		const read = (blob) =>
			new Promise((resolve) => {
				const fr = new FileReader();
				fr.onload = () => resolve(fr.result);
				fr.onerror = () => resolve(null);
				fr.readAsDataURL(blob);
			});
		const out = [];
		for (const s of seqs) {
			const blob = R.thumbs.get(s);
			out.push(blob ? await read(blob) : null);
		}
		return out;
	};

	// Take a keyframe right now from the attended video, for looks that are not tied to a sample.
	R.snapshot = async (width) => {
		const v = R.attended;
		const [vw, vh] = v ? dims(v) : [0, 0];
		if (!v || !vw || R.tainted.has(v)) return null;
		const w = Math.min(width || opts.thumbWidth, vw);
		const h = Math.max(1, Math.round((w * vh) / vw));
		const c = new OffscreenCanvas(w, h);
		c.getContext('2d').drawImage(v, 0, 0, w, h);
		const blob = await c.convertToBlob({ type: 'image/jpeg', quality: 0.8 });
		return await new Promise((resolve) => {
			const fr = new FileReader();
			fr.onload = () => resolve(fr.result);
			fr.readAsDataURL(blob);
		});
	};

	// Hold the attended video still while the agent thinks, so nothing plays unseen; resume
	// picks up exactly where it stopped.
	R.hold = () => {
		const v = R.attended;
		if (v && !v.paused) {
			v.pause();
			R.held = v;
		}
		return R.state();
	};
	R.resume = async () => {
		const v = R.held;
		R.held = null;
		if (v && v.paused) {
			try {
				await v.play();
			} catch (e) {}
		}
		return R.state();
	};

	R.setListen = (on) => {
		opts.listen = !!on;
		if (R.attended && on && !isCanvas(R.attended)) R.attended.muted = false;
		return R.state();
	};

	window.__retina = R;
	if (window.__retinaAutostart) R.start();
})();
