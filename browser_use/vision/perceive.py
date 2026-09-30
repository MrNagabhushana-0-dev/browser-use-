"""Turn a frame into a sentence instead of an image.

A screenshot costs on the order of 1,500 tokens and says everything at once: every
pixel of sky, every pixel of UI chrome, every pixel of the thing that actually matters.
For watching something *move* that is almost all waste, because what a player needs from
a frame is small — what is here, where, and which way it is going.

So this reduces a pair of frames to a short scene description: the blobs that moved,
where they are in normalised coordinates, how big they are, which way they are
travelling, and whether the camera itself is panning. Around sixty tokens, against
fifteen hundred for the picture — and unlike the picture it is already differentiated,
so a caller can act on velocity without having to infer it from two images.

The method is deliberately classical: downsample, difference against the previous frame,
threshold, group adjacent cells, match this frame's groups to last frame's by position.
No model to download, no GPU, around fifteen milliseconds a frame at 1280x800 — most of
which is the JPEG decode, so callers that already hold a decoded frame pay far less. A
segmentation model would
label the blobs ("car", "spike") rather than merely locate them, which is worth having
and is a strictly larger dependency; this is the part that pays for itself immediately.
"""

import base64
import logging
from dataclasses import dataclass, field
from io import BytesIO

logger = logging.getLogger(__name__)

# The grid the frame is reduced to before anything is looked for. Coarse enough that
# JPEG noise and a blinking cursor vanish, fine enough to separate two objects a
# player would treat as separate.
GRID_W = 48
GRID_H = 30

# Per-cell brightness change, 0-255, that counts as "something happened here".
CELL_THRESHOLD = 26

# Cells smaller than this are noise, not objects.
MIN_BLOB_CELLS = 3

# Most blobs a description will mention. Past this it is a scene change, not a scene.
MAX_BLOBS = 6


# Vertical scroll estimation works on its own, finer grid than the blob grid: a shift of a few
# percent of the screen has to be resolvable, and the blob grid's 30 rows cannot do that.
_SCROLL_ROWS = 160
_SCROLL_COLS = 16
# The largest shift considered, as a fraction of the screen. A reader's wheel notches between
# two frames are well inside this; anything bigger is a jump, which reads as a scene change.
_SCROLL_MAX = 0.6
# Below this mean luma difference between frames nothing happened.
_SCROLL_MIN_CHANGE = 3.0
# The winner's error must be at most this fraction of any rival's, or the answer is ambiguous.
_SCROLL_UNIQUENESS = 0.75
# Offsets this close to the winner are the same match seen through a little blur, not rivals.
_SCROLL_NEIGHBOUR = 3
# Mean luma difference that is indistinguishable from JPEG and resampling noise.
_SCROLL_NOISE_FLOOR = 2.0


@dataclass
class Blob:
	"""Something that moved, in normalised 0-1 screen coordinates."""

	x: float
	y: float
	w: float
	h: float
	cells: int
	dx: float = 0.0
	dy: float = 0.0

	@property
	def area(self) -> float:
		return self.w * self.h

	def describe(self) -> str:
		heading = ''
		if abs(self.dx) > 0.01 or abs(self.dy) > 0.01:
			parts = []
			if abs(self.dx) > 0.01:
				parts.append('right' if self.dx > 0 else 'left')
			if abs(self.dy) > 0.01:
				parts.append('down' if self.dy > 0 else 'up')
			heading = ' ' + '-'.join(parts)
		return f'({self.x:.2f},{self.y:.2f}) {self.w:.2f}x{self.h:.2f}{heading}'


@dataclass
class Scene:
	"""What one frame shows, relative to the one before it."""

	motion: int = 0
	blobs: list[Blob] = field(default_factory=list)
	pan: str = ''
	static: bool = False
	# Set when most of the frame changed at once: a new level, a cut, a full-screen card.
	cut: bool = False

	def describe(self) -> str:
		"""The line a player reads instead of looking at the picture."""
		if self.cut:
			return f'scene cut (motion {self.motion})'
		if self.static:
			return 'still — nothing is moving'
		bits = [f'motion {self.motion}']
		if self.pan:
			bits.append(f'camera pans {self.pan}')
		for blob in self.blobs:
			bits.append(blob.describe())
		return '; '.join(bits)


def luma_grid(jpeg_bytes: bytes) -> list[list[int]] | None:
	"""The frame as a coarse brightness grid."""
	try:
		from PIL import Image

		image = Image.open(BytesIO(jpeg_bytes)).convert('L').resize((GRID_W, GRID_H), Image.Resampling.BILINEAR)
	except Exception:
		return None
	pixels = list(image.getdata())  # type: ignore[arg-type]
	return [pixels[row * GRID_W : (row + 1) * GRID_W] for row in range(GRID_H)]


def _group(mask: list[list[bool]]) -> list[Blob]:
	"""Adjacent changed cells, grouped into objects. Flood fill, iterative."""
	seen = [[False] * GRID_W for _ in range(GRID_H)]
	blobs: list[Blob] = []
	for row in range(GRID_H):
		for col in range(GRID_W):
			if not mask[row][col] or seen[row][col]:
				continue
			stack = [(row, col)]
			seen[row][col] = True
			cells = []
			while stack:
				r, c = stack.pop()
				cells.append((r, c))
				for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
					nr, nc = r + dr, c + dc
					if 0 <= nr < GRID_H and 0 <= nc < GRID_W and mask[nr][nc] and not seen[nr][nc]:
						seen[nr][nc] = True
						stack.append((nr, nc))
			if len(cells) < MIN_BLOB_CELLS:
				continue
			rows = [r for r, _ in cells]
			cols = [c for _, c in cells]
			x0, x1 = min(cols), max(cols) + 1
			y0, y1 = min(rows), max(rows) + 1
			blobs.append(
				Blob(
					x=round((x0 + x1) / 2 / GRID_W, 3),
					y=round((y0 + y1) / 2 / GRID_H, 3),
					w=round((x1 - x0) / GRID_W, 3),
					h=round((y1 - y0) / GRID_H, 3),
					cells=len(cells),
				)
			)
	blobs.sort(key=lambda b: b.cells, reverse=True)
	return blobs[:MAX_BLOBS]


def _row_signature(jpeg_bytes: bytes) -> list[list[int]] | None:
	"""One short vector per pixel row, from the central columns only.

	The middle half of the width: a sticky header's edges, a scrollbar, and a side panel that
	scrolls on its own are at the sides and would pin the match at zero shift.
	"""
	try:
		from PIL import Image

		image = Image.open(BytesIO(jpeg_bytes)).convert('L')
		width, height = image.size
		centre = image.crop((width // 4, 0, width * 3 // 4, height)).resize(
			(_SCROLL_COLS, _SCROLL_ROWS), Image.Resampling.BILINEAR
		)
	except Exception:
		return None
	pixels = list(centre.getdata())  # type: ignore[arg-type]
	return [pixels[row * _SCROLL_COLS : (row + 1) * _SCROLL_COLS] for row in range(_SCROLL_ROWS)]


def _row_error(current: list[list[int]], previous: list[list[int]], offset: int, give_up_at: float) -> float | None:
	"""Mean absolute difference between `current` and `previous` shifted by `offset` rows.

	None when the overlap is too small to mean anything, or when the running error already
	exceeds `give_up_at` (so the caller never pays to finish a candidate that cannot win).
	"""
	rows = range(max(0, -offset), min(_SCROLL_ROWS, _SCROLL_ROWS - offset))
	if len(rows) < _SCROLL_ROWS * 0.4:
		return None
	total, limit = 0, give_up_at * len(rows) * _SCROLL_COLS
	for row in rows:
		a, b = current[row], previous[row + offset]
		total += sum(abs(x - y) for x, y in zip(a, b))
		if total > limit:
			return None
	return total / (len(rows) * _SCROLL_COLS)


def scroll_estimate(previous_jpeg: bytes, current_jpeg: bytes) -> tuple[float | None, bool]:
	"""How far the page scrolled between two frames, and whether a refusal is worth telling anyone.

	Returns `(fraction, ambiguous)`. `fraction` is the shift as a fraction of the screen height,
	positive down (the content moved up) and negative up, or None. `ambiguous` is True only when
	some shift fits better than standing still but several fit about equally well: the page very
	probably moved and how far cannot be told. That is different from nothing having moved, and
	from a sprite crossing a fixed page, and a reader keeping a running position needs to know
	which it was, because after an ambiguous step the position can no longer be trusted.

	Units are screens rather than pixels because the stream never needs the viewport: "half a
	screen down" means the same thing on every page.

	Each row of the new frame is compared with the row `offset` away in the old one, and the
	offset that matches best wins. It has to win clearly against every other offset, including
	"no movement", or a blinking cursor, a loading spinner, or a page that repeats itself would be
	reported as scrolling by some amount.
	"""
	previous, current = _row_signature(previous_jpeg), _row_signature(current_jpeg)
	if previous is None or current is None:
		return None, False
	unmoved = _row_error(current, previous, 0, give_up_at=255.0)
	if unmoved is None or unmoved < _SCROLL_MIN_CHANGE:
		return None, False

	best_offset, best_error = 0, unmoved
	reach = int(_SCROLL_ROWS * _SCROLL_MAX)
	for offset in range(-reach, reach + 1):
		if offset == 0:
			continue
		error = _row_error(current, previous, offset, give_up_at=best_error)
		if error is not None and error < best_error:
			best_offset, best_error = offset, error
	if best_offset == 0:
		return None, False

	# The best match must also be *the* match, and standing still counts as a rival (offset 0 is in
	# the loop below), so a shift that is only slightly better than no movement is rejected here.
	# An absolute floor as well as a ratio: a near-perfect match has an error near zero, and a ratio
	# of zero rules out every rival, including ones that fit exactly as well.
	ceiling = max(best_error / _SCROLL_UNIQUENESS, best_error + _SCROLL_NOISE_FLOOR)
	for offset in range(-reach, reach + 1):
		if abs(offset - best_offset) <= _SCROLL_NEIGHBOUR:
			continue
		if _row_error(current, previous, offset, give_up_at=ceiling) is not None:
			return None, True
	return round(best_offset / _SCROLL_ROWS, 3), False


def vertical_scroll(previous_jpeg: bytes, current_jpeg: bytes) -> float | None:
	"""The scroll between two frames as a fraction of the screen height, or None. See `scroll_estimate`."""
	return scroll_estimate(previous_jpeg, current_jpeg)[0]


def _pan(previous: list[list[int]], current: list[list[int]]) -> str:
	"""Whether the whole scene slid, which is the camera moving rather than an object.

	Shift one frame against the other by a few cells and keep the offset that matches
	best. A scrolling runner reads as a steady pan; a static scene with one moving
	sprite does not.
	"""
	band = GRID_H // 2
	best_offset, best_error = 0, None
	for offset in (-3, -2, -1, 0, 1, 2, 3):
		error = total = 0
		for col in range(GRID_W):
			source = col + offset
			if not 0 <= source < GRID_W:
				continue
			error += abs(current[band][col] - previous[band][source])
			total += 1
		if total < GRID_W // 2:
			continue
		mean = error / total
		if best_error is None or mean < best_error:
			best_error, best_offset = mean, offset
	if best_offset == 0 or best_error is None:
		return ''
	return 'left' if best_offset > 0 else 'right'


def perceive(previous_jpeg: bytes | list[list[int]], current_jpeg: bytes | list[list[int]]) -> Scene:
	"""Describe what changed between two frames, in about sixty tokens.

	Either argument may be an already-decoded grid from `luma_grid`, which is how the
	live loop avoids decoding every frame twice.
	"""
	before = previous_jpeg if isinstance(previous_jpeg, list) else luma_grid(previous_jpeg)
	after = current_jpeg if isinstance(current_jpeg, list) else luma_grid(current_jpeg)
	if before is None or after is None:
		return Scene()

	changed = 0
	mask = [[False] * GRID_W for _ in range(GRID_H)]
	total_delta = 0
	for row in range(GRID_H):
		for col in range(GRID_W):
			delta = abs(after[row][col] - before[row][col])
			total_delta += delta
			if delta >= CELL_THRESHOLD:
				mask[row][col] = True
				changed += 1

	cells = GRID_W * GRID_H
	# Percentage of the frame that changed, not the mean change across it. The mean is
	# dominated by themajority of a scene: a car crossing a static background moves a few
	# percent of the cells but barely shifts the average, so a threshold tuned on one is
	# meaningless on the other.
	scene = Scene(motion=round(changed / cells * 100))
	if changed / cells > 0.55:
		scene.cut = True
		return scene
	if changed < MIN_BLOB_CELLS:
		scene.static = True
		return scene

	scene.blobs = _group(mask)
	scene.pan = _pan(before, after)
	return scene


def track(scenes: list[Scene]) -> None:
	"""Fill in each blob's velocity by matching it to the nearest blob in the last scene."""
	for index in range(1, len(scenes)):
		previous = scenes[index - 1].blobs
		if not previous:
			continue
		for blob in scenes[index].blobs:
			nearest = min(previous, key=lambda p: (p.x - blob.x) ** 2 + (p.y - blob.y) ** 2)
			if (nearest.x - blob.x) ** 2 + (nearest.y - blob.y) ** 2 < 0.04:
				blob.dx = round(blob.x - nearest.x, 3)
				blob.dy = round(blob.y - nearest.y, 3)


def frame_to_data_uri(jpeg_bytes: bytes) -> str:
	return 'data:image/jpeg;base64,' + base64.b64encode(jpeg_bytes).decode()
