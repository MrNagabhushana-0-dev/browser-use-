"""Where the one thing that moves goes: turning points of a compact moving object over a still background.

A count of bounces, a pendulum's swings, a cursor sweeping back and forth: the number and timing of
turning points is the information, and no single frame holds it. This reads it from the retina's 16x16
luma samples (about 10 a second). What changed since the previous sample, weighted by how much, is the
object, and its centroid is tracked. A turning point is recorded when the object has come back by more
than HYSTERESIS cells from its extreme, so jitter is not a bounce.

It only speaks when the scene fits: a still background and one compact moving thing (at most
MAX_MOVING_FRACTION of the grid differs in a typical frame). A camera pan or ordinary footage changes
most of the grid and gets no motion line rather than a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from browser_use.eyes.retina import FrameSample
from browser_use.eyes.sight import LOOP_JUMP_S, strays

GRID = 16
DIFF_FLOOR = 12  # luma levels: below this a cell matches the background
MAX_MOVING_FRACTION = 0.15
MIN_TRAVEL = 2.0  # cells: less than this is not an object moving
HYSTERESIS = 0.8  # cells the object must come back by before a turn counts
REST_S = 0.3  # seconds at an extreme with no return: it came to rest there
EPS = 0.1  # cells: centroid jitter that does not move an extreme
WEAK_MASS = 0.25  # of the median change: less is too little movement to place the object
MAX_SPREAD = 3.0  # cells: RMS distance of the change from its centroid for it to be one compact object


@dataclass
class Motion:
	"""Turning points in media time. `bottom`: reached its lowest point and went no lower (bounced or came to rest)."""

	bottom: list[float] = field(default_factory=list)
	top: list[float] = field(default_factory=list)
	left: list[float] = field(default_factory=list)
	right: list[float] = field(default_factory=list)
	rested_at: str | None = None  # 'bottom', 'top', ... when it ended still at an extreme


def _turns(ts: np.ndarray, xs: np.ndarray, t_end: float) -> tuple[list[float], list[float], str | None]:
	"""(times of maxima, times of minima, 'max'/'min' if it ended resting at that extreme) for a 1-D track.

	The start is not a turning point; a turn counts once the track has come back HYSTERESIS from its extreme.
	"""
	maxima: list[float] = []
	minima: list[float] = []
	direction, ext = 0, 0
	for i in range(1, len(xs)):
		if direction == 0:
			if xs[i] - xs[0] > HYSTERESIS:
				direction, ext = 1, int(np.argmax(xs[: i + 1]))
			elif xs[0] - xs[i] > HYSTERESIS:
				direction, ext = -1, int(np.argmin(xs[: i + 1]))
		elif direction == 1:
			if xs[i] > xs[ext] + EPS:
				ext = i
			elif xs[ext] - xs[i] > HYSTERESIS:
				maxima.append(float(ts[ext]))
				direction, ext = -1, i
		else:
			if xs[i] < xs[ext] - EPS:
				ext = i
			elif xs[i] - xs[ext] > HYSTERESIS:
				minima.append(float(ts[ext]))
				direction, ext = 1, i
	rested = None
	if direction and t_end - ts[ext] >= REST_S and abs(xs[-1] - xs[ext]) <= HYSTERESIS:
		j = len(xs) - 1  # it came to rest where the final plateau begins (drift there nudges the extreme)
		while j > 0 and abs(xs[j - 1] - xs[-1]) <= HYSTERESIS / 2:
			j -= 1
		(maxima if direction == 1 else minima).append(float(ts[j]))
		rested = 'max' if direction == 1 else 'min'
	return maxima, minima, rested


def track(frames: list[FrameSample]) -> Motion | None:
	"""Turning points of the single compact moving object in `frames`, or None if the scene is not like that.

	The object is what changed since the previous sample (not what differs from a background: a thing that
	rests most of the time would become background and leave a ghost). Its centroid lags half a sample.
	"""
	# A lone sample stamped at the wrong moment, sorted in by its time, would put a picture from elsewhere in the
	# motion and add a turning point that never happened (`frames` arrive in order, so it can be told apart).
	drop = strays([f.t for f in frames], LOOP_JUMP_S)
	frames = sorted((f for i, f in enumerate(frames) if i not in drop and len(f.luma) == GRID * GRID), key=lambda f: f.t)
	if len(frames) < 8:
		return None
	lum = np.stack([np.frombuffer(f.luma, dtype=np.uint8).reshape(GRID, GRID).astype(np.float32) for f in frames])
	diff = np.abs(np.diff(lum, axis=0))
	weight = np.clip(diff - DIFF_FLOOR, 0, None)
	mass = weight.sum(axis=(1, 2))
	changed = (diff > DIFF_FLOOR).mean(axis=(1, 2))
	ys, xs = np.mgrid[0:GRID, 0:GRID].astype(np.float32) + 0.5
	# A cut or a pan changes most of the grid: that sample is not an object moving, skip it.
	local = (mass > 0) & (changed <= MAX_MOVING_FRACTION)
	if local.sum() < 6:
		return None
	# A sample where little changed (the object nearly still) has a centroid made of noise: skip it too.
	keep = local & (mass > WEAK_MASS * np.median(mass[local]))
	if keep.sum() < 6:
		return None
	# One object: what changed is gathered in one place, not spread over the frame.
	cy_all = (weight[keep] * ys).sum(axis=(1, 2)) / mass[keep]
	cx_all = (weight[keep] * xs).sum(axis=(1, 2)) / mass[keep]
	spread = np.sqrt(
		(weight[keep] * ((ys - cy_all[:, None, None]) ** 2 + (xs - cx_all[:, None, None]) ** 2)).sum(axis=(1, 2)) / mass[keep]
	)
	if np.median(spread) > MAX_SPREAD or keep.sum() < 0.3 * len(frames):
		return None
	t_all = np.array([f.t for f in frames], dtype=np.float64)
	ts = ((t_all[1:] + t_all[:-1]) / 2)[keep]
	cy, cx = cy_all, cx_all
	if max(np.ptp(cy), np.ptp(cx)) < MIN_TRAVEL:
		return None
	low, high, rest_y = _turns(ts, cy, float(t_all[-1]))  # image y grows downward: a maximum is the lowest point
	right, left, rest_x = _turns(ts, cx, float(t_all[-1]))
	rested = {'max': 'bottom', 'min': 'top'}.get(rest_y or '') or {'max': 'right', 'min': 'left'}.get(rest_x or '')
	return Motion(bottom=low, top=high, left=left, right=right, rested_at=rested)


def describe(m: Motion, fmt) -> str:
	"""One line for the percept, e.g. 'reached the bottom 5 times (at 1.2s, ...), the top 4 times; came to rest at the bottom'."""

	def part(name: str, ts: list[float]) -> str:
		at = ', '.join(fmt(t) for t in ts[:12]) + (', ...' if len(ts) > 12 else '')
		return f'the {name} {len(ts)} times (at {at})' if ts else ''

	vertical = [p for p in (part('bottom', m.bottom), part('top', m.top)) if p]
	horizontal = [p for p in (part('left', m.left), part('right', m.right)) if p]
	bits = []
	if vertical:
		bits.append('reached ' + ', '.join(vertical) if not bits else ', '.join(vertical))
	if horizontal:
		bits.append(('reached ' if not bits else 'and ') + ', '.join(horizontal))
	line = 'motion: one moving object; ' + '; '.join(bits) if bits else 'motion: one moving object, no turning points'
	if m.rested_at:
		line += f'; came to rest at the {m.rested_at}'
	return line
