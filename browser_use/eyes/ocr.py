"""Text read from the screen's pixels: what the page's own text cannot tell, such as map labels drawn into tiles, words
in an image or a canvas, or another site's frame.

Found on OpenStreetMap: "City of Westminster" was on screen and find() said "not found", because it searched the
page's text and the names of controls only. This reads the picture instead, with PP-OCR's detection and recognition
models (Apache-2.0) run by RapidOCR on onnxruntime, no PyTorch: the `ocr` extra.

Measured on a 1920x1080 OpenStreetMap frame, 4 CPU cores: ~4.5 s for 198 lines, most of it recognition. Labels came
back with their spaces dropped ("CityofWestminster"), so text is matched with whitespace removed. It is slow next to a
read of the page's text, so find() uses it only when that has no match.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReadLine:
	"""One line of text read from a picture."""

	text: str
	confidence: float  # the recogniser's, 0-1
	box: tuple[float, float, float, float]  # x, y, width, height in the picture's pixels


_engine = None
_lock = threading.Lock()  # one engine, one picture at a time


def available() -> bool:
	try:
		import rapidocr_onnxruntime  # noqa: F401
	except Exception:
		return False
	return True


def read(picture: bytes, looking_for: str = '') -> list[ReadLine]:
	"""The lines of text in a picture (PNG or JPEG bytes). Blocking, seconds for a full screen: run it in a thread.

	With `looking_for`, lines too short to hold it are not read: recognition is most of the time (~25 ms a line), and
	on the OpenStreetMap frame this took "Westminster" from 6.2 s to 4.1 s and "OpenStreetMap" to 2.3 s (no gain for
	a short word, which fits nearly any line). Screen text is upright, so the angle classifier is not run either."""
	global _engine
	import numpy as np
	from PIL import Image

	with Image.open(io.BytesIO(picture)) as img:
		pixels = np.ascontiguousarray(np.array(img.convert('RGB'))[:, :, ::-1])  # the models take OpenCV's channel order
	wanted = len(_squeeze(looking_for))
	with _lock:
		if _engine is None:
			from rapidocr_onnxruntime import RapidOCR

			# A lower box threshold than the default 0.5: map labels crossed by roads were cut short or missed
			# ("Lambeth" read as "eth"); 0.3 found 11 of 12 district names on an OpenStreetMap screen, against 10
			_engine = RapidOCR(det_box_thresh=0.3)
		engine = _engine
		if wanted and all(hasattr(engine, a) for a in ('auto_text_det', 'get_crop_img_list', 'text_rec')):
			# RapidOCR 1.4's own steps, with the lines too short for the query left out between them
			boxes, _ = engine.auto_text_det(pixels)
			boxes = [b for b in (boxes if boxes is not None else []) if _could_hold(b, wanted)]
			texts = engine.text_rec(engine.get_crop_img_list(pixels, boxes))[0] if boxes else []
			result = [(b, t[0], t[1]) for b, t in zip(boxes, texts)]
		else:
			result, _ = engine(pixels, use_cls=False)
	lines = []
	for points, text, score in result or []:
		xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
		lines.append(ReadLine(str(text), float(score), (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))))
	return lines


def _could_hold(box, characters: int) -> bool:
	"""Whether a line box is long enough for that many characters, leniently (a character is ~0.6 of the line's
	height wide; allow it to be 40% shorter than that)."""
	import numpy as np

	width = float(np.linalg.norm(box[1] - box[0]))
	height = max(1.0, float(np.linalg.norm(box[3] - box[0])))
	return width / (0.6 * height) >= 0.6 * characters


def _squeeze(text: str) -> str:
	"""Letters and digits only, in one case: labels come back with spaces dropped and stray marks ("Cityof-Westminster")."""
	return ''.join(c for c in text if c.isalnum()).casefold()


def find(lines: list[ReadLine], query: str) -> list[tuple[ReadLine, bool]]:
	"""The lines that contain `query`, ignoring case, spaces and punctuation, each boxed to the part that matched (an
	estimate from where it falls among the line's characters), and whether all of it was read.

	The detector sometimes cuts the first or last letters off a label ("Southwark" read as "uthwark" on an
	OpenStreetMap screen, at any setting tried). So when no line holds all of the query, a line holding all but up to
	30% of it at one end, at least five characters, counts too: not all read, and boxed out over what was missed."""
	wanted = _squeeze(query)
	assert wanted, 'say what to find'
	found: list[tuple[ReadLine, bool]] = []
	partial: list[tuple[ReadLine, bool]] = []
	for line in lines:
		squeezed = _squeeze(line.text)
		if not squeezed:
			continue
		x, y, w, h = line.box
		per = w / len(squeezed)  # width of a character, roughly
		at = squeezed.find(wanted)
		if at >= 0:
			found.append((ReadLine(line.text, line.confidence, (x + per * at, y, per * len(wanted), h)), True))
			continue
		for cut in range(1, int(len(wanted) * 0.3) + 1):
			if len(wanted) - cut < 5:
				break
			head, tail = wanted[: len(wanted) - cut], wanted[cut:]
			if (at := squeezed.find(tail)) >= 0:  # its start was cut off: it begins before the box
				partial.append((ReadLine(line.text, line.confidence, (x + per * (at - cut), y, per * len(wanted), h)), False))
				break
			if (at := squeezed.find(head)) >= 0:  # its end was cut off
				partial.append((ReadLine(line.text, line.confidence, (x + per * at, y, per * len(wanted), h)), False))
				break
	return found or partial
