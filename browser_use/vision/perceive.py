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
