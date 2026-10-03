"""Turn what the retina gathered into one image and a few lines of text: a percept.

A percept is what an agent actually receives after watching. Its image is a *sheet*: one row
per item watched (a reel, a short), each row a strip of the few keyframes that cover it best,
and under each row a strip of that item's sound, drawn as a spectrogram with the keyframe
times marked on it. Seeing and hearing share one time axis, so "the music drops out when the
product appears" is visible at a glance rather than inferred from two lists.

Its text says what the image cannot: timings, cuts, loops, the sound's labels and tempo, the
words if a speech model heard any, the on-screen caption, and what the image cost.

Token cost is estimated with the documented rule for current models: one token per 28x28
patch after scaling the long edge to at most 2576 px. It is an estimate and labelled as one.
"""

import io
import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from browser_use.eyes import hearing as hearing_mod
from browser_use.eyes import sight as sight_mod
from browser_use.eyes.retina import AudioHop, FrameSample

MAX_LONG_EDGE = 2576
PATCH = 28

# Row height of keyframes on the sheet, by how closely the agent wants to look.
DETAIL_HEIGHT = {'glance': 200, 'look': 320, 'study': 520}
SPECTRUM_HEIGHT = {'glance': 40, 'look': 56, 'study': 72}
GAP = 4
LABEL_BG = (0, 0, 0)
LABEL_FG = (255, 255, 255)


def estimate_image_tokens(width: int, height: int) -> int:
	"""~1 token per 28x28 patch, after the long edge is scaled to <= 2576 px."""
	assert width > 0 and height > 0, 'image dimensions must be positive'
	scale = min(1.0, MAX_LONG_EDGE / max(width, height))
	return math.ceil(width * scale / PATCH) * math.ceil(height * scale / PATCH)


@dataclass
class Keyframe:
	t: float
	seq: int
	jpeg: bytes | None = field(repr=False)


@dataclass
class ItemPercept:
	"""Everything perceived about one attended item (one video) during a watch."""

	index: int
	vid: int
	info: dict  # the retina's 'attend' description: src, size, duration, on-screen text
	frames: list[FrameSample] = field(repr=False)
	hops: list[AudioHop] = field(repr=False)
	sight: sight_mod.Sight = field(repr=False)
	hearing: hearing_mod.Hearing = field(repr=False)
	keyframes: list[Keyframe] = field(default_factory=list)
	coverage: float = 0.0
	watched_s: float = 0.0
	muted: bool | None = None
	tainted: bool = False

	@property
	def t_span(self) -> tuple[float, float]:
		ts = [f.t for f in self.frames] + [h.t for h in self.hops]
		return (min(ts), max(ts)) if ts else (0.0, 0.0)


@dataclass
class Percept:
	items: list[ItemPercept]
	text: str
	image: bytes | None = field(repr=False)  # JPEG
	image_size: tuple[int, int] | None = None
	image_tokens: int = 0
	text_tokens: int = 0
	started_at: float = 0.0
	ended_at: float = 0.0
	stop_reason: str = ''
	frames: list[tuple[float, bytes]] = field(default_factory=list, repr=False)  # (media t, JPEG), from recall

	@property
	def tokens(self) -> int:
		return self.image_tokens + self.text_tokens


# -- text ----------------------------------------------------------------------------------


def _fmt(t: float) -> str:
	return sight_mod.fmt_t(max(0.0, t))


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
	return max(0.0, min(a1, b1) - max(a0, b0))


def describe_item(item: ItemPercept, transcript_chars: int = 600) -> str:
	info = item.info or {}
	lines: list[str] = []
	size = f'{info.get("w")}x{info.get("h")}' if info.get('w') else 'size unknown'
	duration = info.get('duration')
	head = f'[{item.index}] video {size}'
	if duration:
		head += f', {_fmt(duration)} long'
	head += f', watched {item.watched_s:.1f}s'
	if item.sight.loops:
		head += f', looped {len(item.sight.loops)}x (seen in full)'
	lines.append(head)
	text = (info.get('text') or '').strip()
	if text:
		lines.append(f'    on screen: "{text[:240]}"')
	if item.tainted:
		lines.append('    picture: unreadable (cross-origin video without CORS); only sound and on-screen text are known')

	# Pair each shot with the sound under it. After a loop the shots repeat; say so once.
	shots = []
	for shot in item.sight.shots:
		if shot.after_loop:
			break
		if shots and shot.duration < 0.05:  # a single frame at the very end of the watch
			continue
		shots.append(shot)
	for k, shot in enumerate(shots[:12]):
		sounds = [
			s
			for s in item.hearing.segments
			if _overlap(s.t0, s.t1, shot.t0, shot.t1) >= min(0.3, 0.25 * max(shot.duration, 0.01))
		]
		sound = ', '.join(dict.fromkeys(hearing_mod.describe_segment(s) for s in sounds)) or '-'
		opener = 'cut->' if k else 'start'
		lines.append(f'    {_fmt(shot.t0):>6}-{_fmt(shot.t1):<6} {opener:6} {sight_mod.describe_shot(shot):24} | {sound}')
	if len(shots) > 12:
		lines.append(f'    ... {len(shots) - 12} more shots')
	if item.sight.loops:
		lines.append(f'    looped at {_fmt(item.sight.loops[0])}: everything after that is a repeat')
	elif item.sight.rewinds:
		lines.append(f'    rewound at {_fmt(item.sight.rewinds[0])} (seeked back, or restarted by the page); later frames repeat')

	h = item.hearing
	if not h.heard:
		lines.append('    sound: none captured (no audio track, or it had not started)')
	else:
		kinds = ', '.join(h.kinds)
		extra = []
		if h.tempo_bpm and h.tempo_confidence >= 0.3:
			extra.append(f'tempo ~{h.tempo_bpm:.0f} bpm')
		extra.append(f'{len(h.onsets)} onsets')
		if item.muted:
			extra.append('muted for the person watching, heard here from the stream')
		lines.append(f'    sound: {kinds}; {hearing_mod.loudness_word(h.loud_db)}; ' + ', '.join(extra))
		if h.speech_by == 'heuristic' and 'speech' in h.kinds:
			lines.append(
				'    (speech judged by heuristics only, unreliable with music; install the eyes extra for a speech model)'
			)
	if h.transcript:
		said = ' '.join(f'[{_fmt(u.t0)}] {u.text}' for u in h.transcript)
		lines.append(f'    said: {said[:transcript_chars]}{"..." if len(said) > transcript_chars else ""}')
	if item.keyframes:
		ts = ' '.join(_fmt(k.t) for k in item.keyframes)
		lines.append(f'    sheet row {item.index}: {len(item.keyframes)} keyframes at {ts} (cover {item.coverage:.0%} of frames)')
	return '\n'.join(lines)


# -- image ---------------------------------------------------------------------------------


def _font(size: int):
	try:
		return ImageFont.load_default(size=size)
	except TypeError:  # Pillow < 10.1
		return ImageFont.load_default()


def _colour_ramp(v: np.ndarray) -> np.ndarray:
	"""0-1 -> an inferno-like RGB ramp (black, purple, orange, pale yellow)."""
	stops = np.array([[0, 0, 4], [87, 16, 110], [188, 55, 84], [249, 142, 9], [252, 255, 164]], dtype=np.float32)
	x = np.clip(v, 0, 1) * (len(stops) - 1)
	i = np.minimum(x.astype(int), len(stops) - 2)
	f = (x - i)[..., None]
	return (stops[i] * (1 - f) + stops[i + 1] * f).astype(np.uint8)


def spectrogram(hops: list[AudioHop], t0: float, t1: float, width: int, height: int) -> Image.Image:
	"""The hops' 24-band spectrum laid out on [t0, t1], low frequencies at the bottom."""
	img = np.zeros((height, width, 3), dtype=np.uint8)
	if hops and t1 > t0:
		bands = np.frombuffer(b''.join(h.bands for h in hops), dtype=np.uint8).reshape(len(hops), -1)
		ts = np.array([h.t for h in hops])
		cols = np.clip(((ts - t0) / (t1 - t0) * (width - 1)).astype(int), 0, width - 1)
		grid = np.zeros((width, bands.shape[1]), dtype=np.float32)
		count = np.zeros(width, dtype=np.float32)
		np.add.at(grid, cols, bands.astype(np.float32))
		np.add.at(count, cols, 1)
		filled = count > 0
		grid[filled] /= count[filled][:, None]
		# Fill gaps between hops by carrying the previous column forward.
		last = None
		for c in range(width):
			if filled[c]:
				last = grid[c]
			elif last is not None:
				grid[c] = last
		norm = grid / 255.0
		rgb = _colour_ramp(norm)  # (width, bands, 3)
		rows = np.linspace(bands.shape[1] - 1, 0, height).round().astype(int)
		img = rgb[:, rows, :].transpose(1, 0, 2).copy()
	return Image.fromarray(img, 'RGB')


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int) -> None:
	font = _font(size)
	box = draw.textbbox(xy, text, font=font)
	draw.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill=LABEL_BG)
	draw.text(xy, text, fill=LABEL_FG, font=font)


def render_strip(frames: list[tuple[float, bytes]], height: int = 320, gap: int = 6) -> tuple[bytes, int, int] | None:
	"""Frames side by side, each labelled with its media time: what `recall` hands back."""
	images = []
	for t, jpeg in frames:
		with Image.open(io.BytesIO(jpeg)) as img:
			w = max(1, round(img.width * height / max(1, img.height)))
			images.append((t, img.convert('RGB').resize((w, height))))
	if not images:
		return None
	width = sum(img.width for _, img in images) + gap * (len(images) - 1)
	sheet = Image.new('RGB', (width, height), (0, 0, 0))
	draw = ImageDraw.Draw(sheet)
	x = 0
	for t, img in images:
		sheet.paste(img, (x, 0))
		_label(draw, (x + 6, 6), _fmt(t), 14)
		x += img.width + gap
	out = io.BytesIO()
	sheet.save(out, format='JPEG', quality=80)
	return out.getvalue(), width, height


def render_sheet(items: list[ItemPercept], detail: str = 'glance', max_width: int = 1400) -> tuple[bytes, int, int] | None:
	"""One JPEG: a keyframe row plus a sound strip per item. None if there is nothing to show."""
	assert detail in DETAIL_HEIGHT, f'detail must be one of {list(DETAIL_HEIGHT)}'
	rows: list[Image.Image] = []
	for item in items:
		frames = [Image.open(io.BytesIO(k.jpeg)).convert('RGB') for k in item.keyframes if k.jpeg]
		if not frames and not item.hops:
			continue
		h = DETAIL_HEIGHT[detail]
		tiles = [f.resize((max(1, round(f.width * h / f.height)), h), Image.Resampling.LANCZOS) for f in frames]
		strip_w = sum(t.width for t in tiles) + GAP * max(0, len(tiles) - 1)
		# Keep the row within max_width by shrinking the tiles, not by dropping keyframes.
		if strip_w > max_width and tiles:
			k = (max_width - GAP * (len(tiles) - 1)) / sum(t.width for t in tiles)
			tiles = [t.resize((max(1, int(t.width * k)), max(1, int(t.height * k))), Image.Resampling.LANCZOS) for t in tiles]
			strip_w = sum(t.width for t in tiles) + GAP * (len(tiles) - 1)
		strip_w = max(strip_w, 360)
		tile_h = tiles[0].height if tiles else 0
		spec_h = SPECTRUM_HEIGHT[detail] if item.hops else 0
		row = Image.new('RGB', (strip_w, tile_h + (GAP + spec_h if spec_h else 0)), (24, 24, 24))
		draw = ImageDraw.Draw(row)
		font_size = 13 if detail == 'glance' else 16
		x = 0
		for n, (tile, kf) in enumerate(zip(tiles, [k for k in item.keyframes if k.jpeg])):
			row.paste(tile, (x, 0))
			_label(draw, (x + 4, 3), f'{item.index}.{n + 1} {_fmt(kf.t)}', font_size)
			x += tile.width + GAP
		if spec_h:
			t0, t1 = item.t_span
			spec = spectrogram(item.hops, t0, t1, strip_w, spec_h)
			y = tile_h + GAP
			row.paste(spec, (0, y))
			for n, kf in enumerate([k for k in item.keyframes if k.jpeg]):
				if t1 > t0:
					px = int((kf.t - t0) / (t1 - t0) * (strip_w - 1))
					draw.line((px, y, px, y + spec_h), fill=(120, 220, 255), width=1)
					_label(draw, (min(px + 2, strip_w - 30), y + 1), str(n + 1), 11)
			_label(draw, (strip_w - 64, y + spec_h - 15), 'sound', 11)
		if not tiles:
			_label(draw, (4, 2), f'{item.index} (no picture)', font_size)
		rows.append(row)
	if not rows:
		return None
	width = max(r.width for r in rows)
	height = sum(r.height for r in rows) + GAP * (len(rows) - 1)
	sheet = Image.new('RGB', (width, height), (10, 10, 10))
	y = 0
	for r in rows:
		sheet.paste(r, (0, y))
		y += r.height + GAP
	buf = io.BytesIO()
	sheet.save(buf, 'JPEG', quality=82)
	return buf.getvalue(), width, height


def assemble(items: list[ItemPercept], header: str, detail: str = 'glance', footer: str = '') -> Percept:
	"""Text + sheet + token accounting for a list of items."""
	body = '\n'.join(describe_item(i) for i in items) if items else '    (no video was on screen)'
	sheet = render_sheet(items, detail)
	image, size, image_tokens = None, None, 0
	if sheet:
		image, w, h = sheet
		size = (w, h)
		image_tokens = estimate_image_tokens(w, h)
	lines = [header, body]
	if footer:
		lines.append(footer)
	text = '\n'.join(lines)
	text_tokens = max(1, len(text) // 4)
	if image and size:
		text += f'\n~{image_tokens + text_tokens} tokens for this percept (sheet {size[0]}x{size[1]} ~{image_tokens}, text ~{text_tokens}; estimates)'
	return Percept(items, text, image, size, image_tokens, text_tokens)
