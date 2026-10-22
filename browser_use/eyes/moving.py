"""What moved while a step played, read from the frames the page drew: the motion one still frame cannot show.

A model playing a game turn by turn saw only the frame each step ended on, and a still frame shows where things are,
not which way they go or how fast (found when a model played The World's Hardest Game: its dots move on fixed paths,
and it died crossing them). The eyes take frames across the step instead, and from them say what moved, which way and
how fast in game time, and whether the whole view scrolled (a side-scroller's camera following the player).

How (OpenCV's primitives, numpy for the rest). Frames are grey pictures of the game's region, a few hundred px wide, each with the game time it was drawn at,
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
SCROLL_CHANGED = 0.1  # of the picture that differs before a shift of the whole view is looked for: a few moving
# things on a flat background change little of it (~2%), and phase correlation would follow one of them; a scroll seen
# in the compositor's stream (softer than a screenshot) changed 18% of a test world
SCROLL_EXPLAINS = 0.6  # the shift must leave at most this much of the difference between first and last frame
SCROLL_NEAR = 0.25  # of the summed frame-to-frame shift: how far from it the first-to-last shift is looked for
MAX_MOVERS = 12  # said in the answer; more are counted
MIN_TRACK = 3  # looks a thing is followed through to count, once there are more than that
MIN_FRAMES = 3  # distinct frames below which a step is too short to tell what moves (two frames give jumps, not motion)
TOGETHER_MIN = 3  # things going the same way at the same speed that are told as one group (a scrolling world's coins)
TOGETHER_SPREAD = 0.15  # of their speed, or TOGETHER_PX px/s if more, within which two velocities are the same
TOGETHER_PX = 15.0
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
	movers: list[Mover]  # each moving on its own
	scroll: tuple[float, float] | None  # the whole view's velocity, px/s (what it shows moves the other way)
	frames: int  # distinct frames read
	together: list[list[Mover]] = field(default_factory=list)  # groups going the same way at the same speed


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
	offsets = _scrolled(pictures)
	if offsets is not None:
		scroll = (offsets[-1][0] / seconds, offsets[-1][1] / seconds)
		pictures = _aligned(pictures, offsets)
	still = np.median(np.stack(pictures), axis=0)
	blobs = [[(t, *b) for b in _blobs(f, still)] for (t, _), f in zip(frames, pictures)]
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
	movers.sort(key=lambda m: -m.size)
	movers = _one_each(movers)
	movers.sort(key=lambda m: -math.hypot(m.vx, m.vy))
	together, alone = _together(movers)
	return Movement(alone, scroll, len(frames), together)


def heading(dx: float, dy: float) -> str:
	"""right, down-left, ... (screen y grows downwards)."""
	angle = math.degrees(math.atan2(-dy, dx)) % 360
	names = ['right', 'up-right', 'up', 'up-left', 'left', 'down-left', 'down', 'down-right']
	return names[int((angle + 22.5) // 45) % 8]


def describe(movement: Movement, to_picture: float) -> str:
	"""The movement in words, positions and speeds both in the picture the model is shown (`to_picture` of its px per
	frame px): a player that judged moves by the picture misjudged every one by its scale when speeds were in page px."""
	if movement.frames < MIN_FRAMES:
		return (
			f'What the eyes saw move: the step was too short to see what moves ({movement.frames} distinct frames); '
			'step 100 ms or more to see it.'
		)
	parts = []
	if movement.scroll is not None:
		sx, sy = movement.scroll
		parts.append(
			f'the whole view scrolled {heading(-sx, -sy)} at {math.hypot(sx, sy) * to_picture:.0f} px/s '
			f'(what it shows went {heading(sx, sy)})'
		)
	for group in movement.together:
		vx, vy = float(np.mean([m.vx for m in group])), float(np.mean([m.vy for m in group]))
		where = ', '.join(f'({m.x * to_picture:.0f}, {m.y * to_picture:.0f})' for m in group[:MAX_MOVERS])
		parts.append(
			f'{len(group)} things moving together {heading(vx, vy)} at {math.hypot(vx, vy) * to_picture:.0f} px/s, '
			f'at {where}{" ..." if len(group) > MAX_MOVERS else ""}'
		)
	movers = movement.movers
	if not movers:
		if not movement.together:
			parts.append('nothing moved' if movement.scroll is None else 'nothing moved against it')
	else:
		said = [
			f'#{i} at ({m.x * to_picture:.0f}, {m.y * to_picture:.0f}) moving {heading(m.vx, m.vy)} at '
			f'{math.hypot(m.vx, m.vy) * to_picture:.0f} px/s'
			for i, m in enumerate(movers[:MAX_MOVERS], 1)
		]
		more = f' (and {len(movers) - MAX_MOVERS} more)' if len(movers) > MAX_MOVERS else ''
		noun = 'thing' if len(movers) == 1 else 'things'
		alone = ' on its own' if len(movers) == 1 else ' on their own'
		parts.append(f'{len(movers)} {noun} moved{alone if movement.together else ""}{more}: ' + '; '.join(said))
	return (
		f'What the eyes saw move over the step ({movement.frames} distinct frames, game time; positions and speeds in '
		f"the picture's px): " + '; '.join(parts) + '. On the picture each mover has its number, its trail and an arrow '
		'(things moving together: arrows in blue).'
	)


def draw(image, movement: Movement, to_picture: float) -> None:
	"""Each mover's trail, an arrow for where it is heading, and its number, on `image` (a PIL image, in place)."""
	from PIL import ImageDraw

	pen = ImageDraw.Draw(image)
	if movement.frames < MIN_FRAMES:
		return
	for group in movement.together:
		for m in group[:MAX_MOVERS]:
			_arrow(pen, m, to_picture, (0, 160, 255))
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


def _arrow(pen, m: Mover, to_picture: float, colour: tuple[int, int, int]) -> None:
	x, y = m.x * to_picture, m.y * to_picture
	length = math.hypot(m.vx, m.vy) or 1.0
	ux, uy = m.vx / length, m.vy / length
	tip = (x + ux * 16, y + uy * 16)
	pen.line([(x, y), tip], fill=colour, width=2)
	pen.polygon(
		[tip, (tip[0] - ux * 6 - uy * 4, tip[1] - uy * 6 + ux * 4), (tip[0] - ux * 6 + uy * 4, tip[1] - uy * 6 - ux * 4)],
		fill=colour,
	)


def _one_each(movers: list[Mover]) -> list[Mover]:
	"""One mover per thing: a thing that moves less than about twice its size leaves its ghost in the still picture,
	and the hole where the ghost is moves with it, a size behind; the larger of the two is kept (`movers` largest
	first)."""
	kept: list[Mover] = []
	for m in movers:
		same = any(
			math.hypot(m.x - k.x, m.y - k.y) <= max(m.size, k.size) + 3
			and math.hypot(m.vx - k.vx, m.vy - k.vy) <= max(TOGETHER_PX, TOGETHER_SPREAD * math.hypot(k.vx, k.vy))
			for k in kept
		)
		if not same:
			kept.append(m)
	return kept


def _together(movers: list[Mover]) -> tuple[list[list[Mover]], list[Mover]]:
	"""Groups of at least TOGETHER_MIN movers going the same way at the same speed (largest first), and the rest."""
	groups, rest = [], list(movers)
	while True:
		best: list[Mover] = []
		for m in rest:
			tol = max(TOGETHER_PX, TOGETHER_SPREAD * math.hypot(m.vx, m.vy))
			same = [o for o in rest if math.hypot(o.vx - m.vx, o.vy - m.vy) <= tol]
			if len(same) > len(best):
				best = same
		if len(best) < TOGETHER_MIN:
			return groups, rest
		groups.append(best)
		rest = [m for m in rest if m not in best]


def _distinct(frames: list[tuple[float, np.ndarray]]) -> list[tuple[float, np.ndarray]]:
	"""A frame drawn twice in a row (nothing moved between two looks) counts once, at its first time; the last frame
	is always kept, so the read ends where the step did."""
	out = [frames[0]]
	for t, f in frames[1:]:
		if int((np.abs(f - out[-1][1]) > DIFF).sum()) >= MIN_CHANGED:
			out.append((t, f))
	return out


def _scrolled(pictures: list[np.ndarray]) -> list[tuple[float, float]] | None:
	"""Where each frame's view sits from the first's, if the whole view moved: most of the picture changed, and
	moving the last onto the first by the shift found takes most of the difference away. Shifts are found frame to
	frame and summed, which says roughly how far: across the whole step a world that repeats (tiles, stripes) lines up
	again at more than one shift, and phase correlation can pick the wrong one. Each small shift is a little short and
	they add up, so the shift from first to last frame is also looked for near the sum, which says it exactly when it
	is there to be found; whichever of the two lines the last frame up with the first better is taken, and what it
	adds to the sum is spread over the frames."""
	first, last = pictures[0], pictures[-1]
	if float((np.abs(last - first) > DIFF).mean()) < SCROLL_CHANGED:
		return None
	offsets = [(0.0, 0.0)]
	for a, b in zip(pictures, pictures[1:]):
		dx, dy = _shift(a, b)
		offsets.append((offsets[-1][0] + dx, offsets[-1][1] + dy))
	summed = offsets[-1]
	exact = _shift_near(first, last, summed, max(MIN_SCROLL, SCROLL_NEAR * math.hypot(*summed)))
	if exact is not None and _left_over(first, last, exact) < _left_over(first, last, summed):
		(dx, dy), (sx, sy), n = exact, summed, len(offsets) - 1
		offsets = [(ox + (dx - sx) * i / n, oy + (dy - sy) * i / n) for i, (ox, oy) in enumerate(offsets)]
	if math.hypot(*offsets[-1]) < MIN_SCROLL:
		return None
	if _left_over(first, last, offsets[-1]) > SCROLL_EXPLAINS * float(np.abs(last - first).mean()):
		return None
	return offsets


def _left_over(first: np.ndarray, last: np.ndarray, shift: tuple[float, float]) -> float:
	"""How much the last frame still differs from the first once moved back by `shift` (mean grey levels over what
	both show); infinite if they show nothing in common."""
	h, w = first.shape
	sx, sy = int(round(shift[0])), int(round(shift[1]))
	a = first[max(0, -sy) : h - max(0, sy), max(0, -sx) : w - max(0, sx)]
	b = last[max(0, sy) : h - max(0, -sy), max(0, sx) : w - max(0, -sx)]
	return float(np.abs(b - a).mean()) if a.size else math.inf


def _shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
	"""How far b's content sits from a's, (dx, dy) in px, by phase correlation (OpenCV's, Hanning-windowed); (0, 0)
	if no shift is believable."""
	import cv2

	window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
	(dx, dy), response = cv2.phaseCorrelate(a.astype(np.float32), b.astype(np.float32), window)
	if response < SCROLL_PEAK:
		return (0.0, 0.0)
	return (float(dx), float(dy))


def _shift_near(a: np.ndarray, b: np.ndarray, guess: tuple[float, float], radius: float) -> tuple[float, float] | None:
	"""As _shift, but the phase correlation peak is looked for only within `radius` px of `guess`, so a world that
	repeats cannot put it at another of the shifts it lines up at; None if there is no believable peak there."""
	h, w = a.shape
	window = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
	cross = np.fft.fft2(b * window) * np.conj(np.fft.fft2(a * window))
	surface = np.fft.ifft2(cross / (np.abs(cross) + 1e-9)).real  # peak at b's shift from a, wrapped around
	gx, gy, r = round(guess[0]), round(guess[1]), math.ceil(radius)
	ys, xs = np.mgrid[gy - r : gy + r + 1, gx - r : gx + r + 1]
	near = surface[ys % h, xs % w]
	iy, ix = np.unravel_index(int(np.argmax(near)), near.shape)
	py, px = int(ys[iy, ix]), int(xs[iy, ix])
	around = surface[np.arange(py - 1, py + 2)[:, None] % h, np.arange(px - 1, px + 2)[None, :] % w]
	if float(around.sum()) < SCROLL_PEAK:
		return None
	weights = np.clip(around, 0, None)
	total = float(weights.sum())
	if total <= 0:
		return None
	return (
		px + float((weights.sum(axis=0) * [-1, 0, 1]).sum()) / total,
		py + float((weights.sum(axis=1) * [-1, 0, 1]).sum()) / total,
	)


def _aligned(frames: list[np.ndarray], offsets: list[tuple[float, float]]) -> list[np.ndarray]:
	"""The frames moved onto the last one's view, cropped to what all of them show."""
	(ex, ey) = offsets[-1]
	dxs, dys = [int(round(ex - ox)) for ox, _ in offsets], [int(round(ey - oy)) for _, oy in offsets]
	h, w = frames[0].shape
	left, right = max(0, max(dxs)), min(w, w + min(dxs))
	top, bottom = max(0, max(dys)), min(h, h + min(dys))
	if right - left < w // 3 or bottom - top < h // 3:
		return frames
	return [np.roll(f, (dy, dx), axis=(0, 1))[top:bottom, left:right] for f, dx, dy in zip(frames, dxs, dys)]


def _blobs(frame: np.ndarray, still: np.ndarray) -> list[tuple[float, float, float]]:
	"""(x, y, area) of each connected group of what differs between `frame` and the still picture, on a grid of CELL px
	cells (OpenCV's connected components), its centre weighted by how much of each cell differs. A group whose
	difference is in the still picture, not in the frame, is left out: where a slow thing lingered over the step the
	median keeps a faint copy of it, and once the thing has gone the frame there looks like what surrounds it (a ghost,
	read before as a mover of its own)."""
	import cv2

	mask = np.abs(frame - still) > DIFF
	h, w = mask.shape
	gh, gw = h // CELL, w // CELL
	cells = mask[: gh * CELL, : gw * CELL].reshape(gh, CELL, gw, CELL).sum(axis=(1, 3)).astype(np.float32)
	count, labels, stats, _ = cv2.connectedComponentsWithStats((cells > 0).astype(np.uint8), connectivity=4)
	ys, xs = np.indices(cells.shape)
	flat, weights = labels.ravel(), cells.ravel()
	mass = np.bincount(flat, weights=weights, minlength=count)
	sum_x = np.bincount(flat, weights=weights * (xs.ravel() + 0.5), minlength=count)
	sum_y = np.bincount(flat, weights=weights * (ys.ravel() + 0.5), minlength=count)
	return [
		(float(sum_x[i] / mass[i] * CELL), float(sum_y[i] / mass[i] * CELL), float(mass[i]))
		for i in range(1, count)
		if stats[i, cv2.CC_STAT_AREA] >= MIN_BLOB_CELLS and not _ghost(frame, still, mask, labels, stats, i)
	]


def _ghost(frame: np.ndarray, still: np.ndarray, mask: np.ndarray, labels: np.ndarray, stats: np.ndarray, i: int) -> bool:
	"""Whether group `i` of what differs is in the still picture rather than in the frame: against the cells around it
	(what is there in this frame), the still picture's pixels stand out more than the frame's own."""
	x, y, gw, gh = (int(v) for v in stats[i, :4])
	pad = max(2, gw, gh)
	y0, y1, x0, x1 = max(0, y - pad), min(labels.shape[0], y + gh + pad), max(0, x - pad), min(labels.shape[1], x + gw + pad)
	near = labels[y0:y1, x0:x1]
	px = (slice(y0 * CELL, y1 * CELL), slice(x0 * CELL, x1 * CELL))
	group = np.repeat(np.repeat(near == i, CELL, axis=0), CELL, axis=1) & mask[px]
	around = np.repeat(np.repeat(near == 0, CELL, axis=0), CELL, axis=1)
	if not around.any() or not group.any():
		return False
	ground = float(np.median(frame[px][around]))
	return float(np.abs(still[px][group] - ground).mean()) > float(np.abs(frame[px][group] - ground).mean())


def _matched(
	track: list[tuple[float, float, float, float]], pictures: list[np.ndarray], at: dict[float, int]
) -> list[tuple[float, float, float, float]]:
	"""The track with each look's position found by matching the thing's look at the end (a patch of the last picture
	around it) near where its blob was: a blob is only what differs from the still picture, and for a thing slower than
	its own size that is a fragment whose centre lags behind it."""
	import cv2

	_, xe, ye, area = track[-1]
	h, w = pictures[-1].shape
	r = int(max(3, math.sqrt(area) / 2 + 2))
	cx, cy = int(round(xe)), int(round(ye))
	if cx - r < 0 or cy - r < 0 or cx + r >= w or cy + r >= h:
		return track
	template = np.ascontiguousarray(pictures[-1][cy - r : cy + r + 1, cx - r : cx + r + 1], dtype=np.float32)
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
		cost = cv2.matchTemplate(np.ascontiguousarray(window, dtype=np.float32), template, cv2.TM_SQDIFF)
		_, _, (bx, by), _ = cv2.minMaxLoc(cost)
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
