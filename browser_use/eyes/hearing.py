"""What the sound says, without a model: silence, speech, music, a tone, noise, beats.

The retina sends one row of features per 1024-sample hop (~22 ms). This groups them into
half-second windows, labels each window, and merges runs of the same label into segments.
The labels come from the classic hand-built discriminators, not from a classifier:

- *Silence* is energy: below -50 dBFS.
- *A tone* has almost all its energy at one stable frequency: very low spectral flatness,
  a steady envelope, and a peak that does not move.
- *Noise* is the opposite: a flat spectrum (flatness near 1) with a steady envelope.
- *Speech* is syllabic. Its loudness envelope is modulated at about 4 Hz (roughly one
  syllable every 250 ms) and it keeps dropping out between words, so a large share of hops
  sit well below the window's mean energy (the "low-energy rate"). Scheirer & Slaney (1997)
  found these two features among the strongest for separating speech from music.
- *Beats* are regular onsets with near-silence between them (a metronome, a drum loop).
- *Music* is what is left that is loud and sustained; a tempo is estimated from the
  autocorrelation of the onset-strength envelope.

Labels are given per stretch between change points (where the band spectrum or loudness
shifts), not per fixed window, so one sound is never described by its neighbour.

Onsets (a drum hit, a click, a word starting after silence) are peaks of spectral flux that
stand out from the local median by several MADs, or sudden jumps in energy.

These are heuristics and are named as such in every percept. Speech over music, singing,
and applause are exactly where they fail, and a transcript (`asr.py`) is the better
witness for speech when it is available.
"""

from dataclasses import dataclass, field

import numpy as np

from browser_use.eyes.retina import AudioHop
from browser_use.eyes.sight import LOOP_JUMP_S, strays

SILENCE_DB = -50.0
# Half-width of the novelty comparison, and the minimum novelty (band-byte units) for a boundary.
NOVELTY_S = 0.4
NOVELTY_MIN = 10.0
# Longest stretch labelled as one piece. Speech/music features need a couple of seconds to
# mean anything (Scheirer & Slaney's error fell from 5.8% per frame to 1.4% over 2.4 s).
CHUNK_S = 2.5
HZCRR_SPEECH = 0.15
# How far either side of a piece to look for the onsets that make it rhythmic.
BEAT_CONTEXT_S = 1.0
# Unlabelled pieces shorter than this are folded into a neighbour.
SLIVER_S = 1.0
OFFSET_DROP_DB = 6.0
OFFSET_COLLAPSE_DB = 25.0  # the hop after an ending is this much quieter: the sound has stopped  # a flux peak this far below the last few hops' loudness is a sound ending, not starting
ONSET_FLOOR = 0.3
ONSET_MADS = 4.0
ONSET_MIN_GAP_S = 0.08
ENERGY_JUMP_DB = 15.0
TEMPO_MIN_S = 3.0
DEFAULT_HOP_S = 1024 / 48000


@dataclass
class Segment:
	t0: float
	t1: float
	kind: str  # silence | speech | music | tone | noise | sound
	loud_db: float
	detail: str = ''

	@property
	def duration(self) -> float:
		return max(0.0, self.t1 - self.t0)


@dataclass
class Hearing:
	segments: list[Segment] = field(default_factory=list)
	onsets: list[float] = field(default_factory=list)
	tempo_bpm: float | None = None
	tempo_confidence: float = 0.0
	loud_db: float | None = None  # energy-mean loudness of the non-silent part
	peak_db: float | None = None
	hop_s: float = DEFAULT_HOP_S
	hops: int = 0
	speech_by: str = 'heuristic'  # or 'vad' once voice activity detection has been applied
	transcript: list = field(default_factory=list)  # asr.Utterance, when transcribed

	@property
	def heard(self) -> bool:
		return self.hops > 0

	@property
	def kinds(self) -> list[str]:
		seen: list[str] = []
		for s in self.segments:
			if s.kind not in seen:
				seen.append(s.kind)
		return seen


def power_mean_db(db: np.ndarray) -> float:
	if not len(db):
		return -120.0
	return float(10 * np.log10(np.mean(10 ** (db / 10)) + 1e-12))


def modulation_4hz(rms_db: np.ndarray, hop_s: float) -> float:
	"""Share of the loudness envelope's fluctuation energy that lies at 2.5-8 Hz."""
	if len(rms_db) < 16:
		return 0.0
	env = 10 ** (np.maximum(rms_db, -90) / 20)
	env = env - env.mean()
	spectrum = np.abs(np.fft.rfft(env * np.hanning(len(env)))) ** 2
	freqs = np.fft.rfftfreq(len(env), hop_s)
	total = spectrum[freqs > 0.5].sum()
	if total <= 1e-18:
		return 0.0
	return float(spectrum[(freqs >= 2.5) & (freqs <= 8.0)].sum() / total)


def high_zcr_ratio(zcr: np.ndarray) -> float:
	"""Fraction of hops whose zero-crossing rate exceeds 1.5x the stretch's mean (Lu, Zhang & Jiang 2002).

	Speech alternates voiced sounds (low ZCR) with unvoiced consonants (high ZCR); most music
	does not. Measured here on real clips: ~0.21 for a read audiobook, ~0.11 for electronic
	music, with enough overlap per 1.5 s window that it is one vote among several, not a verdict.
	"""
	if not len(zcr) or zcr.mean() <= 0:
		return 0.0
	return float(np.mean(zcr > 1.5 * zcr.mean()))


def low_energy_rate(rms_db: np.ndarray) -> float:
	"""Fraction of hops whose power is below half the window's mean power."""
	if not len(rms_db):
		return 0.0
	p = 10 ** (rms_db / 10)
	return float(np.mean(p < 0.5 * p.mean()))


def classify(
	rms: np.ndarray, flat: np.ndarray, peak: np.ndarray, cen: np.ndarray, hop_s: float, zcr: np.ndarray | None = None
) -> tuple[str, str]:
	"""Label one stretch of hops. Returns (kind, detail)."""
	loud = power_mean_db(rms)
	if loud < SILENCE_DB:
		return 'silence', ''
	# Spectral shape is only defined where there is sound: a click train is mostly silent hops,
	# and their zeros must not outvote the clicks.
	voiced = rms >= SILENCE_DB
	flat_med = float(np.median(flat[voiced]))
	peaks = peak[voiced]
	peak_med = float(np.median(peaks))
	peak_spread = float(np.subtract(*np.percentile(peaks, [75, 25])) / (peak_med + 1e-9)) if peak_med > 0 else 1.0
	# Share of sounding hops within 3 dB of their median loudness: robust to a stray onset, and
	# blind to dropouts (a feed rewinding the video is silence, not evidence against a tone).
	loud_hops = rms[voiced]
	steady = float(np.mean(np.abs(loud_hops - np.median(loud_hops)) < 3.0))
	# A tone is continuous: a train of tonal clicks has the same spectrum but is mostly silence.
	sounding = float(np.mean(voiced))
	if flat_med < 0.02 and steady > 0.8 and peak_spread < 0.03 and sounding > 0.6:
		return 'tone', f'{peak_med:.0f} Hz'
	if flat_med > 0.12 and steady > 0.7 and peak_spread > 0.3:
		return 'noise', ''
	env = 10 ** (rms / 20)
	cv = float(env.std() / (env.mean() + 1e-12))
	mod = modulation_4hz(rms, hop_s)
	ler = low_energy_rate(rms)
	cen_med = float(np.median(cen[voiced]))
	hzcrr = high_zcr_ratio(zcr[voiced]) if zcr is not None else 0.2
	# Voiced speech is harmonic: its spectrum is peaky (flatness well under 0.1; below 0.01 on
	# every real-speech window measured here), where noise is flat (0.15 even through Opus).
	if mod > 0.25 and ler > 0.25 and cv > 0.35 and hzcrr > HZCRR_SPEECH and flat_med < 0.1 and 150 < cen_med < 4500:
		return 'speech', ''
	if cv < 0.6 or ler < 0.2:
		return 'music', ''
	return 'sound', ''


def onsets(hops: list[AudioHop], hop_s: float) -> list[float]:
	if len(hops) < 5:
		return []
	flux = np.array([h.flux for h in hops], dtype=np.float32)
	rms = np.array([h.rms_db for h in hops], dtype=np.float32)
	half = max(3, int(0.5 / hop_s))
	found: list[float] = []
	last = -1e9
	for i in range(1, len(hops)):
		lo, hi = max(0, i - half), min(len(hops), i + half)
		local = flux[lo:hi]
		med = float(np.median(local))
		mad = float(np.median(np.abs(local - med)))
		is_peak = flux[i] >= flux[max(0, i - 2) : i + 3].max()
		# Flux is normalised by the frame's magnitude, so a sound *ending* (magnitude collapsing) spikes it too;
		# only a peak where loudness is not falling well below its recent level is the start of something.
		before = rms[max(0, i - 3) : i].max()
		falling = rms[i] < before - OFFSET_DROP_DB
		# A sound cut off mid-hop smears into a broadband click (high flux) while the hop is still mostly the
		# sound: it is an ending when the sound was already going at this level and the next hop collapses.
		ending = (
			i + 1 < len(hops)
			and before > SILENCE_DB
			and rms[i] >= before - OFFSET_DROP_DB
			and rms[i + 1] < rms[i] - OFFSET_COLLAPSE_DB
		)
		flux_onset = is_peak and flux[i] > max(ONSET_FLOOR, med + ONSET_MADS * mad) and rms[i] > -60 and not (falling or ending)
		energy_onset = rms[i] > SILENCE_DB and rms[i] - rms[max(0, i - 3) : i].min() >= ENERGY_JUMP_DB
		if (flux_onset or energy_onset) and hops[i].t - last >= ONSET_MIN_GAP_S:
			found.append(hops[i].t)
			last = hops[i].t
		elif (flux_onset or energy_onset) and hops[i].t < last:  # media time looped
			found.append(hops[i].t)
			last = hops[i].t
	return found


def tempo(hops: list[AudioHop], hop_s: float) -> tuple[float | None, float]:
	"""(bpm, confidence) from the autocorrelation of the onset-strength envelope."""
	if len(hops) * hop_s < TEMPO_MIN_S:
		return None, 0.0
	env = np.array([h.flux for h in hops], dtype=np.float64)
	env = env - env.mean()
	r0 = float(np.dot(env, env))
	if r0 <= 1e-12:
		return None, 0.0
	lo, hi = int(round(60 / 200 / hop_s)), int(round(60 / 50 / hop_s))
	hi = min(hi, len(env) // 2)
	if hi <= lo + 2:
		return None, 0.0
	ac = np.array([np.dot(env[:-lag], env[lag:]) / r0 for lag in range(lo, hi)])
	j = int(np.argmax(ac))
	confidence = float(ac[j])
	if confidence < 0.2:
		return None, confidence
	lag = lo + j
	if 0 < j < len(ac) - 1:  # parabolic refinement
		a, b, c = ac[j - 1], ac[j], ac[j + 1]
		den = a - 2 * b + c
		if den < 0:
			lag = lo + j + 0.5 * (a - c) / den
	return _fold_bpm(60.0 / (lag * hop_s)), confidence


def _fold_bpm(bpm: float) -> float:
	"""Fold into 70-180 bpm: onset periodicity cannot tell a beat from its half or double."""
	while bpm < 70:
		bpm *= 2
	while bpm > 180:
		bpm /= 2
	return float(bpm)


def change_points(hops: list[AudioHop], hop_s: float) -> list[int]:
	"""Hop indices where the sound changes character (Foote-style novelty on the band spectrum).

	At each hop, compare the mean 24-band spectrum and loudness of the preceding and following
	`NOVELTY_S` seconds; a peak in that difference is a boundary. Classifying the stretches
	between boundaries, rather than fixed windows, keeps a tone from being described by the
	clicks that follow it.
	"""
	n = len(hops)
	w = max(4, int(round(NOVELTY_S / hop_s)))
	if n < 2 * w + 1:
		return []
	bands = np.frombuffer(b''.join(h.bands for h in hops), dtype=np.uint8).reshape(n, -1).astype(np.float32)
	rms = np.array([max(h.rms_db, -90.0) for h in hops], dtype=np.float32)
	csum = np.vstack([np.zeros((1, bands.shape[1]), np.float32), np.cumsum(bands, axis=0)])
	rsum = np.concatenate([[0.0], np.cumsum(rms)])
	novelty = np.zeros(n, dtype=np.float32)
	for i in range(w, n - w):
		left = (csum[i] - csum[i - w]) / w
		right = (csum[i + w] - csum[i]) / w
		loud = abs((rsum[i + w] - rsum[i]) - (rsum[i] - rsum[i - w])) / w
		novelty[i] = float(np.abs(left - right).mean()) + 0.5 * loud
	points: list[int] = []
	for i in range(w, n - w):
		if novelty[i] < NOVELTY_MIN or novelty[i] < novelty[max(0, i - w) : i + w + 1].max():
			continue
		if points and i - points[-1] < w:
			continue
		points.append(i)
	return points


def _regular(times: list[float]) -> float | None:
	"""Median inter-onset interval if the onsets are evenly spaced, else None."""
	if len(times) < 4:
		return None
	ioi = np.diff(np.array(sorted(times)))
	ioi = ioi[ioi > 0.05]
	if len(ioi) < 3:
		return None
	med = float(np.median(ioi))
	return med if float(np.std(ioi) / med) < 0.15 else None


def absorb_beat_edges(segments: list[Segment], onsets: list[float], tolerance: float = 0.15) -> list[Segment]:
	"""Fold a 'sound' segment into the 'beats' segment it touches when its onsets fall on that beat's grid.

	A change point can land inside a click track and leave its first or last second on its own: too few onsets
	there to show a rhythm, so it is labelled plain 'sound'. Every onset of it within `tolerance` of a period of
	the neighbour's beat makes it the same beats. A piece with no onsets, or any off the grid, stays as it is.
	"""
	out = list(segments)
	i = 0
	while i < len(out):
		seg = out[i]
		mine = [t for t in onsets if seg.t0 <= t < seg.t1]
		if seg.kind != 'sound' or not mine:
			i += 1
			continue
		for j in (i + 1, i - 1):
			if not 0 <= j < len(out) or out[j].kind != 'beats':
				continue
			nb = out[j]
			if abs((nb.t0 if j > i else nb.t1) - (seg.t1 if j > i else seg.t0)) > 0.1:
				continue  # not touching
			theirs = [t for t in onsets if nb.t0 <= t <= nb.t1]
			# Judged together: the sound piece's onsets continue the beat when the joined run is still even.
			period = _regular(sorted(theirs + mine))
			if period is None or not theirs:
				continue
			anchor = theirs[0]
			if all(abs((t - anchor) / period - round((t - anchor) / period)) <= tolerance for t in mine):
				nb.t0, nb.t1 = min(nb.t0, seg.t0), max(nb.t1, seg.t1)
				nb.loud_db = max(nb.loud_db, seg.loud_db)
				del out[i]
				break
		else:
			i += 1
	return out


def _smooth(segments: list[Segment]) -> list[Segment]:
	"""Fold short unlabelled slivers into a neighbour: a change point lands a hop or two off,
	and the piece it leaves behind is a mix of both sides that deserves no label of its own."""
	out: list[Segment] = []
	for i, seg in enumerate(segments):
		sliver = (seg.kind == 'sound' and seg.duration < SLIVER_S) or (
			seg.kind == 'music' and seg.duration < 0.6 and not seg.detail
		)
		if sliver and (out or i + 1 < len(segments)):
			nxt = segments[i + 1] if i + 1 < len(segments) else None
			if nxt is not None and nxt.kind != 'silence':
				nxt.t0 = seg.t0
				continue
			if out:
				out[-1].t1 = seg.t1
				continue
		# Adjacent beats (or music) are one run even if their pieces guessed slightly different tempi:
		# the caller re-estimates one tempo from all of the run's onsets, as listen() itself merges them.
		if out and out[-1].kind == seg.kind and (out[-1].detail == seg.detail or seg.kind in ('beats', 'music')):
			out[-1].t1 = seg.t1
			continue
		out.append(seg)
	return out


def listen(hops: list[AudioHop], sample_rate: float | None = None) -> Hearing:
	"""Read one item's audio hops (in arrival order)."""
	hop_s = 1024 / sample_rate if sample_rate else DEFAULT_HOP_S
	if not hops:
		return Hearing(hop_s=hop_s)
	drop = strays([h.t for h in hops], LOOP_JUMP_S)
	hops = [h for i, h in enumerate(hops) if i not in drop]
	rms = np.array([h.rms_db for h in hops], dtype=np.float32)
	flat = np.clip(np.array([h.flatness for h in hops], dtype=np.float64), 0, 1)
	peak = np.array([h.peak_hz for h in hops], dtype=np.float32)
	cen = np.array([h.centroid_hz for h in hops], dtype=np.float32)
	zcr = np.array([h.zcr for h in hops], dtype=np.float32)
	found = onsets(hops, hop_s)

	# Media time restarting (a loop) is a boundary too.
	loops = [i for i in range(1, len(hops)) if hops[i].t < hops[i - 1].t - LOOP_JUMP_S]
	bounds = sorted(set([0, *change_points(hops, hop_s), *loops, len(hops)]))
	chunk = max(4, int(round(CHUNK_S / hop_s)))

	segments: list[Segment] = []
	for a, b in zip(bounds, bounds[1:]):
		# Long stretches are labelled in pieces: speech and music can alternate without the
		# spectrum changing enough to make a boundary.
		pieces = list(range(a, b, chunk))
		if len(pieces) > 1 and b - pieces[-1] < chunk // 2:
			pieces.pop()
		for k, lo in enumerate(pieces):
			hi = pieces[k + 1] if k + 1 < len(pieces) else b
			t0, t1 = hops[lo].t, hops[hi - 1].t + hop_s
			kind, detail = classify(rms[lo:hi], flat[lo:hi], peak[lo:hi], cen[lo:hi], hop_s, zcr[lo:hi])
			if kind in ('sound', 'music', 'speech'):
				# Regularity needs several onsets, so judge it over a neighbourhood of the piece.
				# Evenly spaced onsets are rhythm; syllables never are, so this also overrules a
				# heuristic 'speech' (pumping electronic music fools the envelope features).
				period = _regular([t for t in found if t0 - BEAT_CONTEXT_S <= t <= t1 + BEAT_CONTEXT_S])
				if period is not None and sum(1 for t in found if t0 <= t <= t1) >= 2:
					bpm_here = _fold_bpm(60 / period)
					kind = 'beats' if low_energy_rate(rms[lo:hi]) > 0.5 else 'music'
					detail = f'~{bpm_here:.0f} bpm'
			loud = power_mean_db(rms[lo:hi])
			prev = segments[-1] if segments else None
			same = prev is not None and prev.kind == kind and (prev.detail == detail or kind in ('beats', 'music'))
			if prev and same and t0 >= prev.t0:
				prev.t1 = t1
				prev.loud_db = max(prev.loud_db, loud)
			else:
				segments.append(Segment(t0, t1, kind, loud, detail))

	segments = absorb_beat_edges(_smooth(segments), found)
	for s in segments:  # a merged run of beats gets one tempo, from all of its onsets
		if s.kind in ('beats', 'music'):
			period = _regular([t for t in found if s.t0 - 0.05 <= t <= s.t1])
			if period is not None:
				s.detail = f'~{_fold_bpm(60 / period):.0f} bpm'

	voiced = rms[rms >= SILENCE_DB]
	bpm, conf = tempo(hops, hop_s)
	for s in segments:
		# A tempo needs beats to hang on: at least one onset per ~1.25 s of the segment.
		inside = sum(1 for t in found if s.t0 <= t <= s.t1)
		if (
			s.kind == 'music'
			and bpm is not None
			and s.duration >= TEMPO_MIN_S
			and not s.detail
			and inside >= max(4, 0.8 * s.duration)
		):
			s.detail = f'~{bpm:.0f} bpm'
	if len(found) < 4:
		bpm, conf = None, 0.0
	return Hearing(
		segments=segments,
		onsets=found,
		tempo_bpm=bpm if len(voiced) else None,
		tempo_confidence=conf,
		loud_db=power_mean_db(voiced) if len(voiced) else None,
		peak_db=float(rms.max()),
		hop_s=hop_s,
		hops=len(hops),
	)


def apply_speech_regions(hearing: Hearing, regions: list[tuple[float, float]]) -> Hearing:
	"""Let a voice-activity model decide where the speech is, keeping everything else.

	Non-silent segments are split at the model's region edges: the parts inside a region
	become speech, and parts outside that the heuristics had called speech become 'sound'.
	"""
	out: list[Segment] = []
	for seg in hearing.segments:
		if seg.kind == 'silence':
			out.append(seg)
			continue
		cuts = sorted({seg.t0, seg.t1, *[t for a, b in regions for t in (a, b) if seg.t0 < t < seg.t1]})
		for a, b in zip(cuts, cuts[1:]):
			mid = (a + b) / 2
			inside = any(r0 <= mid <= r1 for r0, r1 in regions)
			if inside:
				kind, detail = 'speech', ''
			elif seg.kind == 'speech':
				kind, detail = 'sound', ''
			else:
				kind, detail = seg.kind, seg.detail
			prev = out[-1] if out else None
			if prev and prev.kind == kind and prev.detail == detail and abs(prev.t1 - a) < 0.05:
				prev.t1 = b
			else:
				out.append(Segment(a, b, kind, seg.loud_db, detail))
	# Relabelling makes new 'sound' slivers (heuristic 'speech' the model rejected): fold them like any other.
	hearing.segments = _smooth(out)
	hearing.speech_by = 'vad'
	return hearing


def loudness_word(db: float | None) -> str:
	if db is None:
		return 'silent'
	if db > -14:
		return 'loud'
	if db > -26:
		return 'moderate'
	if db > -40:
		return 'quiet'
	return 'very quiet'


def describe_segment(s: Segment) -> str:
	if s.kind == 'silence':
		return 'silence'
	extra = f' {s.detail}' if s.detail else ''
	return f'{s.kind}{extra} ({loudness_word(s.loud_db)})'
