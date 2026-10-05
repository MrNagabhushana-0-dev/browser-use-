"""What the frames say: shots, cuts, motion, colour, and which few frames to actually look at.

Everything here works on the retina's 16x16 luma grids, so it is cheap enough to run on
every batch. The one image-level decision, *which* keyframes to spend image tokens on, is
made here too, and it is the part worth explaining.

Choosing keyframes is a covering problem. Every sampled frame should be represented on the
contact sheet by some keyframe that looks like it; a reel that holds one shot for eight
seconds and flashes three others needs four keyframes, not eight of the long shot. That is
the *facility location* objective

    F(S) = (1/n) * sum_i max_{s in S} sim(i, s)

over the sampled frames i, with sim(i, s) = exp(-(d(i, s) / sigma)^2) on the grid distance d.
F is monotone submodular, so the greedy choice (repeatedly add the frame with the largest
gain) is within a factor (1 - 1/e) of the best possible sheet of the same size (Nemhauser,
Wolsey & Fisher, 1978). Its value is also a meaningful number to report: F = 0.93 means the
sheet stands in for 93% of what was on screen, by this similarity.

The same quantity gives a principled notion of boredom. The *marginal* coverage of the last
few seconds, how much F would gain from adding their best frame, falls towards zero once a
reel stops showing anything new. `novelty()` returns that, and the watcher moves on when it
stays low.

Limits: a grid this small sees composition and brightness, not detail. Two talking heads in
the same framing look alike; text changing on a static background is nearly invisible. A
slow fade is reported as a cut where it changes fastest, or not at all.
"""

import colorsys
from dataclasses import dataclass, field

import numpy as np

from browser_use.eyes.retina import FrameSample

# Mean absolute luma difference (0-255) between consecutive samples. A hard cut between
# unrelated shots scores 25-90; camera motion and moving subjects score 2-15.
CUT_FLOOR = 16.0
# ...and must also stand out from the recent run of differences by this many MADs, so a
# shaky handheld clip that differs by 14 every frame does not cut on every frame.
CUT_MADS = 6.0
# Differences considered "recent" for that comparison, in samples.
CUT_WINDOW = 20
# Shots shorter than this are merged into the previous one: a flash, not a shot.
MIN_SHOT_S = 0.25
# Media time going backwards by more than this is a loop (or a seek back).
LOOP_JUMP_S = 0.4
# ...unless it was still within its first second: that is the feed restarting it on arrival.
RESTART_S = 1.0
# A backward jump from within this many seconds of the end is the video looping.
LOOP_END_S = 0.6
# Width of the similarity kernel, in grid-distance units.
SIGMA = 14.0

# Upper hue bound (degrees) of each colour name, in order around the wheel.
_HUES = (
	(15, 'red'),
	(45, 'orange'),
	(70, 'yellow'),
	(160, 'green'),
	(200, 'cyan'),
	(255, 'blue'),
	(285, 'purple'),
	(330, 'magenta'),
	(345, 'pink'),
	(361, 'red'),
)


@dataclass
class Shot:
	"""A run of frames between two cuts."""

	start: int  # index into the frame list
	end: int  # exclusive
	t0: float
	t1: float
	motion: float  # median consecutive-frame difference inside the shot
	brightness: float  # mean luma, 0-255
	colour: str
	cut_strength: float = 0.0  # the difference that opened this shot (0 for the first)
	colourfulness: float = 0.0
	hues: list[str] = field(default_factory=list)
	after_loop: bool = False

	@property
	def duration(self) -> float:
		return max(0.0, self.t1 - self.t0)

	@property
	def motion_word(self) -> str:
		return motion_word(self.motion)


@dataclass
class Sight:
	"""The visual reading of one item's frames."""

	frames: list[FrameSample] = field(repr=False)
	deltas: np.ndarray = field(repr=False)
	shots: list[Shot] = field(default_factory=list)
	loops: list[float] = field(default_factory=list)  # media times at which the item started over from its end
	rewinds: list[float] = field(default_factory=list)  # media times at which it jumped back otherwise

	@property
	def cuts(self) -> list[float]:
		return [s.t0 for s in self.shots[1:] if not s.after_loop]


def motion_word(motion: float) -> str:
	if motion < 1.0:
		return 'still'
	if motion < 3.0:
		return 'calm'
	if motion < 8.0:
		return 'moving'
	return 'busy'


def colour_name(rgb: tuple[int, int, int] | np.ndarray) -> str:
	"""The colour a person would say, from the hue (HSV), with lightness words for the unsaturated and dark.

	Hue sectors, not nearest swatch in RGB: the nearest-swatch namer called pure cyan "teal".
	"""
	r, g, b = (float(x) for x in rgb)
	top, bottom = max(r, g, b), min(r, g, b)
	if top - bottom < 28:  # unsaturated: name it by lightness alone
		return 'black' if top < 45 else 'white' if bottom > 200 else 'dark grey' if top < 100 else 'grey'
	h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
	hue = h * 360
	if 10 <= hue < 45 and v < 0.6:
		return 'brown'
	name = next(n for limit, n in _HUES if hue < limit)
	if name in ('red', 'magenta') and s < 0.6 and v > 0.7:
		name = 'pink'
	return f'dark {name}' if v < 0.55 else name


def colourfulness(frames: list[FrameSample]) -> tuple[float, list[str]]:
	"""(Hasler & Susstrunk colourfulness, dominant hue names) over the frames' 4x4 colour grids.

	M = sqrt(var(rg) + var(yb)) + 0.3 * sqrt(mean(rg)^2 + mean(yb)^2), with rg = R - G and
	yb = (R + G)/2 - B. On full-resolution photos ~0 is grey, ~33 moderately colourful, 80+
	extremely; on a 4x4 grid fine detail averages out, so values read low and only the
	coarse categories are used.
	"""
	grids_ = [np.frombuffer(f.colours, dtype=np.uint8).reshape(-1, 3).astype(np.float32) for f in frames if len(f.colours) == 48]
	if not grids_:
		return 0.0, []
	rgb = np.concatenate(grids_)
	r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
	rg, yb = r - g, (r + g) / 2 - b
	m = float(np.sqrt(rg.var() + yb.var()) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))
	top, bottom = rgb.max(axis=1), rgb.min(axis=1)
	sat = (top - bottom) / np.maximum(top, 1)
	vivid = rgb[(sat > 0.35) & (top > 60)]
	counts: dict[str, int] = {}
	for cell in vivid:
		name = colour_name(cell)
		name = name.replace('dark ', '')
		counts[name] = counts.get(name, 0) + 1
	hues = [n for n, c in sorted(counts.items(), key=lambda kv: -kv[1]) if c >= max(2, 0.12 * len(vivid))][:2]
	return m, hues


def grids(frames: list[FrameSample]) -> np.ndarray:
	"""(n, 256) float32 luma matrix."""
	if not frames:
		return np.zeros((0, 256), dtype=np.float32)
	return np.frombuffer(b''.join(f.luma for f in frames), dtype=np.uint8).reshape(len(frames), -1).astype(np.float32)


def deltas(matrix: np.ndarray) -> np.ndarray:
	"""Mean absolute difference of each frame from the one before (0 for the first)."""
	if len(matrix) < 2:
		return np.zeros(len(matrix), dtype=np.float32)
	d = np.abs(np.diff(matrix, axis=0)).mean(axis=1)
	return np.concatenate([[0.0], d]).astype(np.float32)


def read(frames: list[FrameSample], duration: float | None = None) -> Sight:
	"""Segment one item's frames (in arrival order) into shots."""
	matrix = grids(frames)
	d = deltas(matrix)
	starts: list[tuple[int, float, bool]] = [(0, 0.0, False)] if frames else []
	loops: list[float] = []
	rewinds: list[float] = []
	for i in range(1, len(frames)):
		if frames[i].t < frames[i - 1].t - LOOP_JUMP_S:
			if frames[i - 1].t < RESTART_S:
				# Feeds rewind a video when it scrolls into view; that is a restart, not a loop.
				continue
			# A loop starts over from the end; anything else going backwards is a rewind (a
			# seek, or the page restarting it). Without a known duration, assume a loop.
			if duration and frames[i - 1].t < duration - LOOP_END_S:
				rewinds.append(frames[i - 1].t)
			else:
				loops.append(frames[i - 1].t)
			starts.append((i, float(d[i]), True))
			continue
		recent = d[max(1, i - CUT_WINDOW) : i]
		if len(recent) >= 3:
			med = float(np.median(recent))
			mad = float(np.median(np.abs(recent - med))) + 0.5
			threshold = max(CUT_FLOOR, med + CUT_MADS * mad)
		else:
			threshold = CUT_FLOOR
		if d[i] >= threshold:
			last_start = starts[-1][0]
			if frames[i].t - frames[last_start].t < MIN_SHOT_S and last_start != 0 and not starts[-1][2]:
				# Too soon after the previous cut: keep one cut, at the stronger change.
				if d[i] > starts[-1][1]:
					starts[-1] = (i, float(d[i]), False)
				continue
			starts.append((i, float(d[i]), False))

	shots: list[Shot] = []
	for k, (start, strength, after_loop) in enumerate(starts):
		end = starts[k + 1][0] if k + 1 < len(starts) else len(frames)
		inner = d[start + 1 : end]
		mean_rgb = np.mean([frames[j].rgb for j in range(start, end)], axis=0)
		t1 = frames[end].t if end < len(frames) and not starts[k + 1][2] else frames[end - 1].t
		shots.append(
			Shot(
				start=start,
				end=end,
				t0=frames[start].t,
				t1=t1,
				motion=float(np.median(inner)) if len(inner) else 0.0,
				brightness=float(matrix[start:end].mean()),
				colour=colour_name(mean_rgb),
				cut_strength=strength,
				after_loop=after_loop,
				colourfulness=(cf := colourfulness(frames[start:end]))[0],
				hues=cf[1],
			)
		)
	return Sight(frames=frames, deltas=d, shots=shots, loops=loops, rewinds=rewinds)


def similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
	"""exp(-(d/sigma)^2) between every row of a and every row of b."""
	dist = np.abs(a[:, None, :] - b[None, :, :]).mean(axis=2)
	return np.exp(-((dist / SIGMA) ** 2))


@dataclass
class Selection:
	indices: list[int]  # into the frame list, in time order
	coverage: float  # F(S), 0-1
	gains: list[float]  # marginal gain of each pick, in pick order


def select_keyframes(frames: list[FrameSample], k: int, min_gain: float = 0.01) -> Selection:
	"""Greedy facility location over the frames that have keyframes in the page's ring."""
	candidates = [i for i, f in enumerate(frames) if f.has_keyframe]
	if not frames or not candidates or k <= 0:
		return Selection([], 0.0, [])
	matrix = grids(frames)
	sim = similarity(matrix, matrix[candidates])  # (n, c)
	n = len(frames)
	best = np.zeros(n, dtype=np.float32)
	chosen: list[int] = []
	gains: list[float] = []
	for _ in range(min(k, len(candidates))):
		gain = np.maximum(sim - best[:, None], 0).sum(axis=0) / n
		if chosen:
			gain[[candidates.index(c) for c in chosen]] = -1
		j = int(np.argmax(gain))
		if gain[j] < min_gain:
			break
		chosen.append(candidates[j])
		gains.append(float(gain[j]))
		best = np.maximum(best, sim[:, j])
	return Selection(sorted(chosen), float(best.mean()), gains)


def novelty(seen: list[FrameSample], recent: list[FrameSample]) -> float:
	"""How much the recent frames add to what `seen` already covers (0 = nothing new, 1 = all new).

	The mean over recent frames of (1 - best similarity to anything seen before).
	"""
	if not recent:
		return 0.0
	if not seen:
		return 1.0
	step = max(1, len(seen) // 200)  # bound the cost on long watches
	sim = similarity(grids(recent), grids(seen[::step]))
	return float(1.0 - sim.max(axis=1).mean())


def describe_shot(shot: Shot) -> str:
	light = 'dark' if shot.brightness < 60 else 'bright' if shot.brightness > 190 else ''
	if shot.colourfulness or shot.hues:  # measured on the colour grid
		if shot.colourfulness < 6 and not shot.hues:
			look = 'black-and-white'
		elif shot.hues:
			look = '/'.join(shot.hues)
		else:
			look = 'muted colours'
		look = f'{light} {look}'.strip()
	else:  # no colour grid (older retina): fall back to the mean colour
		look = shot.colour if not light or light in shot.colour else f'{light} {shot.colour}'
	return f'{look}, {shot.motion_word}'


def fmt_t(t: float) -> str:
	if t < 60:
		return f'{t:.1f}s'
	m, s = divmod(t, 60)
	return f'{int(m)}:{s:04.1f}'
