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
		return best;
	};

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

	const describe = (v) => ({
		vid: v === R.attended ? R.attendedId : 0,
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

	// An *item* is one thing watched: a new element, or the same element given a new source
	// (virtualized feeds recycle a few <video> elements for every reel).
	const attend = () => {
		const v = pick();
		const src = v ? String(v.currentSrc || v.src || '') : '';
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
		if (v) {
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
		if (!v.videoWidth) return;
		const w = Math.min(opts.thumbWidth, v.videoWidth);
		const h = Math.max(1, Math.round((w * v.videoHeight) / v.videoWidth));
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
			const mt = meta.mediaTime;
			// A backwards jump is a loop or a seek: always sample it.
			if (mt >= R.lastSampleT && mt - R.lastSampleT < 1 / opts.fps) return;
			R.lastSampleT = mt;
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
		};
		v.requestVideoFrameCallback(onFrame);
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
	};

	const startHearing = async (v) => {
		stopHearing();
		try {
			const node = await ensureEar();
			if (R.attended !== v) return;
			if (!v.__retinaStream) v.__retinaStream = v.captureStream();
			const tracks = v.__retinaStream.getAudioTracks();
			if (!tracks.length) {
				// Media Source players add their audio track after the first segment; retried
				// by the heartbeat.
				R.audioMode = 'no-track';
				return;
			}
			R.audioSource = R.ctx.createMediaStreamSource(new MediaStream(tracks));
			R.audioSource.connect(node);
			R.audioMode = 'worklet';
		} catch (e) {
			R.audioMode = 'error: ' + String((e && e.message) || e).slice(0, 120);
		}
	};

	// -- plumbing ------------------------------------------------------------------------

	const heartbeat = () => {
		const v = R.attended;
		if (R.ctx && R.ctx.state === 'suspended') R.ctx.resume().catch(() => {});
		if (v && opts.audio && (R.audioMode === 'no-track' || R.audioMode.startsWith('error'))) startHearing(v);
		if (v && opts.listen && v.muted) v.muted = false;
		R.events.push({
			type: 'state',
			wt: performance.now(),
			vid: R.attendedId,
			t: v ? v.currentTime : null,
			paused: v ? v.paused : null,
			muted: v ? v.muted || v.volume === 0 : null,
			audio: R.audioMode,
			ctx: R.ctx ? R.ctx.state : 'none',
			sr: R.ctx ? R.ctx.sampleRate : null,
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
		if (!v || !v.videoWidth || R.tainted.has(v)) return null;
		const w = Math.min(width || opts.thumbWidth, v.videoWidth);
		const h = Math.max(1, Math.round((w * v.videoHeight) / v.videoWidth));
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
		if (R.attended && on) R.attended.muted = false;
		return R.state();
	};

	window.__retina = R;
	if (window.__retinaAutostart) R.start();
})();
