"""Motion profiles that make synthetic input look like it came from a hand.

The point is not aesthetics. A pointer that teleports to an element's centre, clicks with
zero dwell and releases in the same millisecond produces an event stream no hand can
produce: no `mousemove` before the `mousedown`, no `mouseover` on the way in, identical
down/up timestamps, pixel-perfect centre coordinates every time. Sites that care —
Instagram, YouTube, anything with a bot-risk score — read exactly those signals, and so do
ordinary UIs: a hover menu that never receives `mouseover` simply never opens, and the
click lands on whatever was underneath.

So the curves here exist to drive real intermediate events, and the jitter exists so the
landing point is not always the exact geometric centre.

Everything is deterministic under a seeded Random, which is what makes it testable.
"""

import math
import random

# Fitts's law, loosely: bigger jumps take longer, but sublinearly, and nothing is instant.
MIN_MOVE_MS = 60.0
MAX_MOVE_MS = 700.0

# How far the pointer may drift from the straight line, as a fraction of the distance.
# Real pointer paths bow; they do not follow a ruler.
BOW_FRACTION = 0.12

# Per-step wobble in CSS pixels. Small: a hand is unsteady, not drunk.
JITTER_PX = 0.6


def move_duration_ms(distance: float, rng: random.Random) -> float:
	"""How long a hand would take to cover `distance` pixels."""
	if distance <= 0:
		return MIN_MOVE_MS
	# Fitts-like: time grows with log of distance, plus a little variance per person/moment.
	base = 90.0 * math.log2(1.0 + distance / 40.0)
	return float(min(MAX_MOVE_MS, max(MIN_MOVE_MS, base * rng.uniform(0.8, 1.35))))


def step_count(distance: float) -> int:
	"""Enough intermediate points that the page sees a real move, not a jump.

	Capped because every step is a CDP round trip: past roughly 40 the motion looks no
	more human and the click just gets slower.
	"""
	return int(min(40, max(6, distance / 18.0)))


def _ease(t: float) -> float:
	"""Ease-in-out. A hand accelerates away and decelerates onto the target."""
	return 3 * t * t - 2 * t * t * t


def bezier_path(
	start: tuple[float, float],
	end: tuple[float, float],
	rng: random.Random,
	steps: int | None = None,
) -> list[tuple[float, float]]:
	"""A bowed, slightly unsteady path from `start` to `end`, inclusive of both.

	Control points sit off the straight line on a consistent side, so the path bows the
	way an arm swings rather than zig-zagging.
	"""
	x0, y0 = start
	x1, y1 = end
	distance = math.hypot(x1 - x0, y1 - y0)
	n = steps if steps is not None else step_count(distance)
	if distance < 1.0:
		return [(x1, y1)]

	# Unit normal to the direction of travel, used to push the control points aside.
	nx, ny = -(y1 - y0) / distance, (x1 - x0) / distance
	bow = distance * BOW_FRACTION * rng.uniform(0.4, 1.0) * rng.choice((-1.0, 1.0))

	# Two control points at roughly a third and two thirds, both pushed off-line.
	c1 = (x0 + (x1 - x0) / 3 + nx * bow, y0 + (y1 - y0) / 3 + ny * bow)
	c2 = (x0 + 2 * (x1 - x0) / 3 + nx * bow * 0.6, y0 + 2 * (y1 - y0) / 3 + ny * bow * 0.6)

	path: list[tuple[float, float]] = []
	for i in range(1, n + 1):
		t = _ease(i / n)
		u = 1 - t
		x = u**3 * x0 + 3 * u**2 * t * c1[0] + 3 * u * t**2 * c2[0] + t**3 * x1
		y = u**3 * y0 + 3 * u**2 * t * c1[1] + 3 * u * t**2 * c2[1] + t**3 * y1
		if i < n:
			# Never jitter the final point: the click must land where it was aimed.
			x += rng.gauss(0, JITTER_PX)
			y += rng.gauss(0, JITTER_PX)
		path.append((x, y))
	return path


def landing_point(x: float, y: float, width: float, height: float, rng: random.Random) -> tuple[float, float]:
	"""Where inside an element a hand actually lands.

	Biased to the middle but never exactly centre, and always comfortably inside the box,
	so a one-pixel border or an overlapping child does not swallow the click.
	"""
	dx = rng.gauss(0, max(1.0, width / 8))
	dy = rng.gauss(0, max(1.0, height / 8))
	limit_x = max(0.0, width / 2 - 2)
	limit_y = max(0.0, height / 2 - 2)
	return x + max(-limit_x, min(limit_x, dx)), y + max(-limit_y, min(limit_y, dy))


def click_dwell_ms(rng: random.Random) -> float:
	"""How long a button stays held down. Humans are not instantaneous."""
	return rng.uniform(45.0, 130.0)


def keystroke_delays(text: str, rng: random.Random, wpm: float = 260.0) -> list[float]:
	"""Per-character pauses in milliseconds.

	Not a constant: space after a word runs long, repeated characters run short, and the
	occasional character catches. A fixed inter-key delay is one of the easiest synthetic
	signatures to spot in an event log.
	"""
	assert wpm > 0, 'wpm must be positive'
	base = 60_000.0 / (wpm * 5.0)  # 5 chars per "word", by convention
	delays: list[float] = []
	previous = ''
	for char in text:
		d = base * rng.uniform(0.55, 1.6)
		if char == ' ':
			d *= 1.35
		elif char in '.,!?;:':
			d *= 1.5
		elif char == previous:
			d *= 0.72
		elif char.isupper():
			d *= 1.25  # the shift reach costs something
		if rng.random() < 0.04:
			d += rng.uniform(120.0, 320.0)  # a moment's hesitation
		delays.append(d)
		previous = char
	return delays
