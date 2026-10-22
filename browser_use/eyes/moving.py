"""What moved while a step played, read from the frames the page drew: the motion one still frame cannot show.

A model playing a game turn by turn saw only the frame each step ended on, and a still frame shows where things are,
not which way they go or how fast (found when a model played The World's Hardest Game: its dots move on fixed paths,
and it died crossing them). The eyes take frames across the step instead, and from them say what moved, which way and
how fast in game time, and whether the whole view scrolled (a side-scroller's camera following the player).

How. Frames are grey pictures of the game's region, a few hundred px wide, each with the game time it was drawn at,
in order, the first from before the step. The view's own movement is found by phase correlation of the first and the
last frame and taken out. What stands still is the per-pixel median of the frames; what differs from it is grouped
into blobs, and each blob is followed from frame to frame by its nearest successor. What is said is what is there as
the step ends, heading the way it went over its last few looks (a dot that turned during the step goes the way it went
last), and only things that went at least MIN_TRAVEL px. Something that moved less than its own size is only partly
told apart from what stands still, so its speed reads low; its direction is still right.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

DIFF = 28  # grey levels from what stands still that count as something there
CELL = 2  # px: what differs is grouped on a grid of cells this size
MIN_BLOB_CELLS = 2  # smaller is noise
MIN_TRAVEL = 2.0  # px over the step: less is standing still
MAX_LINK = 0.2  # of the picture's width: the farthest a thing is followed from one frame to the next
MIN_CHANGED = 6  # px that differ for a frame to differ from the one before
MIN_SCROLL = 2.0  # px over the step for the view itself to have moved
SCROLL_PEAK = 0.08  # phase correlation peak height for a shift of the whole view to be believed
SCROLL_CHANGED = 0.25  # of the picture that differs before a shift of the whole view is looked for: a few moving
# things on a flat background change little of it, and phase correlation would follow one of them
SCROLL_EXPLAINS = 0.6  # the shift must leave at most this much of the difference between first and last frame
MAX_MOVERS = 12  # said in the answer; more are counted
MIN_TRACK = 3  # looks a thing is followed through to count, once there are more than that
HEADING_LOOKS = 3  # the last looks that say which way a thing was going as the step ended
JITTER = 1.0  # px a centroid wanders by without the thing turning


@dataclass
class Mover:
	"""One thing that moved: where it ended, and its velocity, in the frames' px and px per second of game time."""

	x: float
	y: float
	vx: float
	vy: float
	size: float  # px across (square root of its area)
	trail: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Movement:
	movers: list[Mover]
	scroll: tuple[float, float] | None  # the whole view's velocity, px/s (what it shows moves the other way)
	frames: int  # distinct frames read


def read(frames: list[tuple[float, np.ndarray]]) -> Movement:
	"""What moved across `frames`: (game time in ms, grey picture), all one size, in order, the first from before the
	step and the last from its end."""
	assert frames and all(f.shape == frames[0][1].shape for _, f in frames), 'frames of one size'
	frames = _distinct([(t, f.astype(np.float32)) for t, f in frames])
	if len(frames) < 2:
		return Movement([], None, len(frames))
	seconds = max(frames[-1][0] - frames[0][0], 1.0) / 1000
	scroll = None
	pictures = [f for _, f in frames]
	shift = _scrolled(pictures[0], pictures[-1])
	if shift is not None:
		scroll = (shift[0] / seconds, shift[1] / seconds)
		pictures = _aligned(pictures)
	still = np.median(np.stack(pictures), axis=0)
	blobs = [[(t, *b) for b in _blobs(np.abs(f - still) > DIFF)] for (t, _), f in zip(frames, pictures)]
	end = frames[-1][0]
	shortest = MIN_TRACK if len(frames) >= MIN_TRACK + 1 else 2
	movers = []
	at = {t: i for i, (t, _) in enumerate(frames)}
	for track in _follow(blobs, width=pictures[0].shape[1]):
		# what is there as the step ends, seen long enough not to be a flicker
		if track[-1][0] != end or len(track) < shortest:
			continue
		track = _matched(track, pictures, at)
		velocity = _last_leg(track)
		if velocity is None:
			continue
		_, x1, y1, area = track[-1]
		movers.append(Mover(x1, y1, *velocity, math.sqrt(area), [(x, y) for _, x, y, _ in track]))
	movers.sort(key=lambda m: -math.hypot(m.vx, m.vy))
	return Movement(movers, scroll, len(frames))


def heading(dx: float, dy: float) -> str:
	"""right, down-left, ... (screen y grows downwards)."""
	angle = math.degrees(math.atan2(-dy, dx)) % 360
	names = ['right', 'up-right', 'up', 'up-left', 'left', 'down-left', 'down', 'down-right']
	return names[int((angle + 22.5) // 45) % 8]


def describe(movement: Movement, to_picture: float, to_css: float) -> str:
	"""The movement in words: positions in the picture the model is shown (`to_picture` per frame px), speeds in the
	page's CSS px per second of game time (`to_css` per frame px)."""
	parts = []
	if movement.scroll is not None:
		sx, sy = movement.scroll
		parts.append(
			f'the whole view scrolled {heading(-sx, -sy)} at {math.hypot(sx, sy) * to_css:.0f} px/s '
			f'(what it shows went {heading(sx, sy)})'
		)
	movers = movement.movers
	if not movers:
		parts.append('nothing moved' if movement.scroll is None else 'nothing moved against it')
	else:
		said = [
			f'#{i} at ({m.x * to_picture:.0f}, {m.y * to_picture:.0f}) moving {heading(m.vx, m.vy)} at '
			f'{math.hypot(m.vx, m.vy) * to_css:.0f} px/s'
			for i, m in enumerate(movers[:MAX_MOVERS], 1)
		]
		more = f' (and {len(movers) - MAX_MOVERS} more)' if len(movers) > MAX_MOVERS else ''
		noun = 'thing' if len(movers) == 1 else 'things'
		parts.append(f'{len(movers)} {noun} moved{more}: ' + '; '.join(said))
	return (
		f'What the eyes saw move over the step ({movement.frames} distinct frames, game time; positions in the picture, '
		f'speeds in page px/s): ' + '; '.join(parts) + '. On the picture each mover has its number, its trail and an arrow.'
	)


def draw(image, movement: Movement, to_picture: float) -> None:
	"""Each mover's trail, an arrow for where it is heading, and its number, on `image` (a PIL image, in place)."""
	from PIL import ImageDraw

	pen = ImageDraw.Draw(image)
	for i, m in enumerate(movement.movers[:MAX_MOVERS], 1):
		trail = [(x * to_picture, y * to_picture) for x, y in m.trail]
		if len(trail) >= 2:
			pen.line(trail, fill=(255, 220, 0), width=3)
		x, y = m.x * to_picture, m.y * to_picture
		length = math.hypot(m.vx, m.vy) or 1.0
		ux, uy = m.vx / length, m.vy / length
		reach = max(18.0, m.size * to_picture)
		tip = (x + ux * reach, y + uy * reach)
		pen.line([(x, y), tip], fill=(255, 0, 255), width=3)
		left = (tip[0] - ux * 8 - uy * 6, tip[1] - uy * 8 + ux * 6)
		right = (tip[0] - ux * 8 + uy * 6, tip[1] - uy * 8 - ux * 6)
		pen.polygon([tip, left, right], fill=(255, 0, 255))
		pen.text((x + 6, y - 16), f'#{i}', fill=(255, 0, 255))


def _distinct(frames: list[tuple[float, np.ndarray]]) -> list[tuple[float, np.ndarray]]:
	"""A frame drawn twice in a row (nothing moved between two looks) counts once, at its first time; the last frame
	is always kept, so the read ends where the step did."""
	out = [frames[0]]
	for t, f in frames[1:]:
		if int((np.abs(f - out[-1][1]) > DIFF).sum()) >= MIN_CHANGED:
			out.append((t, f))
	return out


def _scrolled(first: np.ndarray, last: np.ndarray) -> tuple[float, float] | None:
	"""How far the whole view moved between two frames, if it did: most of the picture changed, and moving one onto
	the other by the shift found takes most of the difference away."""
	if float((np.abs(last - first) > DIFF).mean()) < SCROLL_CHANGED:
		return None
	dx, dy = _shift(first, last)
	if math.hypot(dx, dy) < MIN_SCROLL:
		return None
	h, w = first.shape
	sx, sy = int(round(dx)), int(round(dy))
	a = first[max(0, -sy) : h - max(0, sy), max(0, -sx) : w - max(0, sx)]
	b = last[max(0, sy) : h - max(0, -sy), max(0, sx) : w - max(0, -sx)]
	if a.size == 0 or float(np.abs(b - a).mean()) > SCROLL_EXPLAINS * float(np.abs(last - first).mean()):
		return None
	return (dx, dy)


def _shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
	"""How far b's content sits from a's, (dx, dy) in px, by phase correlation; (0, 0) if no shift is believable."""
	h, w = a.shape
	window = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
	fa, fb = np.fft.fft2((a - a.mean()) * window), np.fft.fft2((b - b.mean()) * window)
	cross = fb * np.conj(fa)
	cross /= np.abs(cross) + 1e-6
	surface = np.fft.ifft2(cross).real
	peak = np.unravel_index(int(np.argmax(surface)), surface.shape)
	if surface[peak] < SCROLL_PEAK or surface[0, 0] >= surface[peak] * 0.9:
		return (0.0, 0.0)
	dy, dx = (p if p <= n // 2 else p - n for p, n in zip(peak, surface.shape))
	return (float(dx), float(dy))


def _aligned(frames: list[np.ndarray]) -> list[np.ndarray]:
	"""The frames moved onto the last one's view, cropped to what all of them show."""
	shifts = [_shift(f, frames[-1]) for f in frames]
	dxs, dys = [int(round(s[0])) for s in shifts], [int(round(s[1])) for s in shifts]
	h, w = frames[0].shape
	left, right = max(0, max(dxs)), min(w, w + min(dxs))
	top, bottom = max(0, max(dys)), min(h, h + min(dys))
	if right - left < w // 3 or bottom - top < h // 3:
		return frames
	return [np.roll(f, (dy, dx), axis=(0, 1))[top:bottom, left:right] for f, dx, dy in zip(frames, dxs, dys)]


def _blobs(mask: np.ndarray) -> list[tuple[float, float, float]]:
	"""(x, y, area) of each connected group of what differs, its cells CELL px square."""
	h, w = mask.shape
	gh, gw = h // CELL, w // CELL
	cells = mask[: gh * CELL, : gw * CELL].reshape(gh, CELL, gw, CELL).sum(axis=(1, 3))
	on = cells > 0
	seen = np.zeros_like(on)
	blobs = []
	for start in zip(*np.nonzero(on)):
		if seen[start]:
			continue
		seen[start] = True
		stack, members = [start], []
		while stack:
			cy, cx = stack.pop()
			members.append((cy, cx))
			for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
				if 0 <= ny < gh and 0 <= nx < gw and on[ny, nx] and not seen[ny, nx]:
					seen[ny, nx] = True
					stack.append((ny, nx))
		if len(members) < MIN_BLOB_CELLS:
			continue
		weights = np.array([cells[c] for c in members], dtype=np.float32)
		ys = (np.array([c[0] for c in members]) + 0.5) * CELL
		xs = (np.array([c[1] for c in members]) + 0.5) * CELL
		blobs.append(
			(float((xs * weights).sum() / weights.sum()), float((ys * weights).sum() / weights.sum()), float(weights.sum()))
		)
	return blobs


def _matched(
	track: list[tuple[float, float, float, float]], pictures: list[np.ndarray], at: dict[float, int]
) -> list[tuple[float, float, float, float]]:
	"""The track with each look's position found by matching the thing's look at the end (a patch of the last picture
	around it) near where its blob was: a blob is only what differs from the still picture, and for a thing slower than
	its own size that is a fragment whose centre lags behind it."""
	_, xe, ye, area = track[-1]
	h, w = pictures[-1].shape
	r = int(max(3, math.sqrt(area) / 2 + 2))
	cx, cy = int(round(xe)), int(round(ye))
	if cx - r < 0 or cy - r < 0 or cx + r >= w or cy + r >= h:
		return track
	template = pictures[-1][cy - r : cy + r + 1, cx - r : cx + r + 1]
	out = []
	for t, x, y, a in track:
		picture = pictures[at[t]]
		gx, gy, s = int(round(x)), int(round(y)), r
		x0, y0 = max(0, gx - r - s), max(0, gy - r - s)
		x1, y1 = min(w, gx + r + s + 1), min(h, gy + r + s + 1)
		window = picture[y0:y1, x0:x1]
		if window.shape[0] < 2 * r + 1 or window.shape[1] < 2 * r + 1:
			out.append((t, x, y, a))
			continue
		views = np.lib.stride_tricks.sliding_window_view(window, template.shape)
		cost = ((views - template) ** 2).sum(axis=(2, 3))
		by, bx = np.unravel_index(int(np.argmin(cost)), cost.shape)
		out.append((t, float(x0 + bx + r), float(y0 + by + r), a))
	return out


def _last_leg(track: list[tuple[float, float, float, float]]) -> tuple[float, float] | None:
	"""Velocity (px/s) of a tracked thing as the step ends: a straight-line fit over the stretch back from the end in
	which it kept going the way it went last (a thing that turned is read from after its turn). None if it went less
	than MIN_TRAVEL px over that stretch."""
	ts = np.array([p[0] for p in track]) / 1000
	xs, ys = np.array([p[1] for p in track]), np.array([p[2] for p in track])
	recent = max(0, len(track) - 1 - HEADING_LOOKS)
	ux, uy = xs[-1] - xs[recent], ys[-1] - ys[recent]
	norm = math.hypot(ux, uy)
	if norm < 1e-6:
		return None
	along = (xs * ux + ys * uy) / norm  # how far along the last heading each look found it
	start = len(track) - 1
	while start > 0 and along[start - 1] <= along[start] + JITTER:
		start -= 1
	if along[-1] - along[start] < MIN_TRAVEL or ts[-1] <= ts[start]:
		return None
	t = ts[start:] - ts[start:].mean()
	return float((t * xs[start:]).sum() / (t * t).sum()), float((t * ys[start:]).sum() / (t * t).sum())


def _follow(
	per_frame: list[list[tuple[float, float, float, float]]], width: int
) -> list[list[tuple[float, float, float, float]]]:
	"""Tracks of blobs (time, x, y, area) over the frames: each blob is joined, nearest first, to a track that had a blob
	in the frame before; one left over starts a track of its own, and a track that gets none ends."""
	reach = MAX_LINK * width
	tracks = [[b] for b in per_frame[0]]
	active = list(range(len(tracks)))
	for blobs in per_frame[1:]:
		pairs = sorted(
			(math.hypot(b[1] - tracks[t][-1][1], b[2] - tracks[t][-1][2]), t, j) for t in active for j, b in enumerate(blobs)
		)
		joined: set[int] = set()
		taken: set[int] = set()
		for distance, t, j in pairs:
			if distance <= reach and t not in joined and j not in taken:
				tracks[t].append(blobs[j])
				joined.add(t)
				taken.add(j)
		active = sorted(joined)
		for j, b in enumerate(blobs):
			if j not in taken:
				tracks.append([b])
				active.append(len(tracks) - 1)
	return [t for t in tracks if len(t) >= 2]
