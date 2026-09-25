"""Give each tracked region a coarse label, not just a position.

`(0.21, 0.62)` tells a reader where something is. It does not tell them what it is, and
"there is a thing moving in the lower left" is a weaker sentence than "a button is in the
lower left." A screenshot carries the what for free — at fifteen hundred tokens. The point
of the perception stream is to carry it for a few.

This does it without a model to download, from three cheap visual features computed over a
region:

- edge density — text and dense UI have many small luminance transitions; a photo, a flat
  panel, or a solid button have few. This is the strongest single signal for "is this
  text".
- colour variance — a video frame or a photo varies wildly across its area; a button, an
  icon, a panel are close to flat. This separates media from chrome.
- shape and size — a wide, short region is a text line or a bar; a small square is an icon;
  a large region is a panel or a stage.

The labels are coarse on purpose: text, button, icon, media, panel, region. A finer
taxonomy ("submit button" vs "nav button", icon vs button) is a job for a segmentation model, which is a
strictly larger dependency; this is the part that pays for itself with no weights at all.
Every label is a guess from pixels and is named as one — a caller that needs certainty
looks, a caller that needs a hint reads this.
"""

import logging
from io import BytesIO

logger = logging.getLogger(__name__)

# The grids the features are read from. Luma is finer than the motion grid because edge
# density needs resolution to mean anything; colour is coarse because variance does not.
LUMA_W = 96
LUMA_H = 60
RGB_W = 48
RGB_H = 30

Label = str  # one of: text, button, media, panel, region

# Thresholds, tuned on real captured frames (games, a video, dashboards). Edge density is
# mean absolute neighbour difference over 0-255; colour variance is mean per-channel range
# over 0-255. Both are scale-free, so they transfer across page and viewport sizes.
_TEXT_EDGE = 28
_FLAT_EDGE = 12
_MEDIA_VARIANCE = 46
_FLAT_VARIANCE = 20


class FrameFeatures:
	"""Decoded once per frame; queried per region.

	Decoding a JPEG is the expensive part, so it happens here and every blob in the frame
	is labelled against the same two grids rather than re-decoding for each.
	"""

	def __init__(self, luma: list[list[int]] | None, rgb: list[list[tuple[int, int, int]]] | None) -> None:
		self.luma = luma
		self.rgb = rgb

	@property
	def usable(self) -> bool:
		return self.luma is not None and self.rgb is not None

	def label(self, x: float, y: float, w: float, h: float) -> Label:
		"""A coarse category for the region at normalised (x, y, w, h), centre-origin.

		x, y are the centre in 0-1; w, h are the extent in 0-1. Falls back to 'region' for
		anything it cannot decode or cannot tell apart, because a wrong specific label is
		worse than an honest vague one.
		"""
		if not self.usable:
			return 'region'
		assert self.luma is not None and self.rgb is not None

		left, right = x - w / 2, x + w / 2
		top, bottom = y - h / 2, y + h / 2
		edge = _edge_density(self.luma, LUMA_W, LUMA_H, left, top, right, bottom)
		variance = _colour_variance(self.rgb, RGB_W, RGB_H, left, top, right, bottom)
		area = max(0.0, min(1.0, w)) * max(0.0, min(1.0, h))

		# Big and busy is a video or a photo; big and flat is the page or a panel behind it.
		if area >= 0.15:
			return 'media' if variance >= _MEDIA_VARIANCE else 'panel'
		# Many small transitions over a modest area reads as text, whatever its shape.
		if edge >= _TEXT_EDGE:
			return 'text'
		# Flat and low-variance, below panel size: a solid control — a button, or an icon,
		# which is just a small button. They are not separable at this grid resolution (a
		# tiny block cannot have an interior cell that avoids its own border), so they share
		# one honest label rather than a distinction the pixels cannot support.
		if edge <= _FLAT_EDGE and variance <= _FLAT_VARIANCE:
			return 'button'
		return 'region'


def _rows(pixels, width: int, height: int):
	return [pixels[r * width : (r + 1) * width] for r in range(height)]


def frame_features(jpeg_bytes: bytes) -> FrameFeatures:
	"""Decode a frame into the two grids the labeller reads. Never raises."""
	try:
		from PIL import Image

		image = Image.open(BytesIO(jpeg_bytes))
		luma_img = image.convert('L').resize((LUMA_W, LUMA_H), Image.Resampling.BILINEAR)
		rgb_img = image.convert('RGB').resize((RGB_W, RGB_H), Image.Resampling.BILINEAR)
	except Exception:
		return FrameFeatures(None, None)
	luma = _rows(list(luma_img.getdata()), LUMA_W, LUMA_H)  # type: ignore[arg-type]
	rgb = _rows(list(rgb_img.getdata()), RGB_W, RGB_H)  # type: ignore[arg-type]
	return FrameFeatures(luma, rgb)


def _bounds(width: int, height: int, left: float, top: float, right: float, bottom: float):
	"""Clamp a normalised box to grid indices, always at least one cell wide."""
	x0 = max(0, min(width - 1, int(left * width)))
	x1 = max(x0 + 1, min(width, int(right * width) + 1))
	y0 = max(0, min(height - 1, int(top * height)))
	y1 = max(y0 + 1, min(height, int(bottom * height) + 1))
	return x0, x1, y0, y1


def _edge_density(luma, width: int, height: int, left: float, top: float, right: float, bottom: float) -> float:
	x0, x1, y0, y1 = _bounds(width, height, left, top, right, bottom)
	total = count = 0
	for r in range(y0, y1):
		row = luma[r]
		for c in range(x0, x1):
			here = row[c]
			if c + 1 < x1:
				total += abs(here - row[c + 1])
				count += 1
			if r + 1 < y1:
				total += abs(here - luma[r + 1][c])
				count += 1
	return total / count if count else 0.0


def _colour_variance(rgb, width: int, height: int, left: float, top: float, right: float, bottom: float) -> float:
	x0, x1, y0, y1 = _bounds(width, height, left, top, right, bottom)
	mins = [255, 255, 255]
	maxs = [0, 0, 0]
	seen = False
	for r in range(y0, y1):
		row = rgb[r]
		for c in range(x0, x1):
			seen = True
			pixel = row[c]
			for ch in range(3):
				value = pixel[ch]
				if value < mins[ch]:
					mins[ch] = value
				if value > maxs[ch]:
					maxs[ch] = value
	if not seen:
		return 0.0
	return sum(maxs[ch] - mins[ch] for ch in range(3)) / 3.0
