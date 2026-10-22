"""Ground truth for event-coverage scoring (C2): objective cuts, film alignment, contact sheets, frozen labels.

Everything here reads the screen recording only (never a watcher's narration), so it can run before any scoring.
Definitions of C1/C2 are fixed in docs/contract/LOOP_MEMORY.md; the procedure is docs/contract/PROTOCOL_v3_P2.md.

usage (run with `uv run --no-sync python scripts/contract/ground_truth.py ...`):
  cuts REC.mp4 [--from S --to S] [--out cuts.json]       scene cuts in recording seconds, each with its score
  align cuts.json --edits EDITS.json [--max-offset 60]   rec_s = film_s + offset that matches the most cuts
  sheets REC.mp4 --out DIR [--every 0.5] [--from S --to S] [--cuts cuts.json]   contact sheets + DIR/index.json
  template --out labels.json [--recording REC] ...       the label file a labeller fills in
  freeze labels.json                                     validate, print sha256 (logged before any scoring)

Decoding is PyAV (in the repo venv). Deterministic: same file and arguments, same output.
"""

import argparse
import hashlib
import json
import math
import os
import stat
import statistics
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

SMALL = (160, 90)  # every frame is reduced to this (area filter) for cut detection and region finding
GRID = (16, 9)  # luma grid over the moving region
STILL = 0.004  # a frame whose score is under this shows no change (compression noise only)
PLACEHOLDER = 'REPLACE ME'


def _version() -> str:
	"""This script's own content hash, put in every output so a number can be traced to the code that made it."""
	return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def _sha256(path: str | Path) -> str:
	h = hashlib.sha256()
	with open(path, 'rb') as f:
		for block in iter(lambda: f.read(1 << 20), b''):
			h.update(block)
	return h.hexdigest()


def _emit(data: dict, out: str | None) -> None:
	text = json.dumps(data, indent=1)
	if out:
		Path(out).parent.mkdir(parents=True, exist_ok=True)
		Path(out).write_text(text + '\n')
	print(text)


# --- decoding ----------------------------------------------------------------------------------------------------------


def _frames(path: str, start: float | None, end: float | None) -> Iterator[tuple[float, av.VideoFrame]]:
	"""Decoded frames (presentation time in seconds, frame) with start <= t <= end."""
	with av.open(path, mode='r') as container:
		stream = container.streams.video[0]
		stream.thread_type = 'AUTO'
		rate = float(stream.average_rate or 30)
		if start:
			container.seek(int(max(0.0, start - 2.0) / stream.time_base), stream=stream, backward=True)
		for n, frame in enumerate(container.decode(stream)):
			t = float(frame.time) if frame.time is not None else n / rate
			if start is not None and t < start - 1e-6:
				continue
			if end is not None and t > end + 1e-6:
				break
			yield t, frame


def _probe(path: str | Path) -> tuple[tuple[int, int], float]:
	"""(width, height) of the video stream and the file's duration in seconds."""
	with av.open(str(path), mode='r') as container:
		stream = container.streams.video[0]
		return (stream.codec_context.width, stream.codec_context.height), float(container.duration or 0) / 1e6


def _scan(path: str, start: float | None, end: float | None) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
	"""Every frame in the span reduced to SMALL (RGB, area filter): (times, frames n x h x w x 3, source width/height)."""
	times, small, size = [], [], (0, 0)
	for t, frame in _frames(path, start, end):
		size = (frame.width, frame.height)
		times.append(t)
		small.append(frame.to_ndarray(width=SMALL[0], height=SMALL[1], format='rgb24', interpolation='AREA'))
	assert small, f'no frames decoded from {path} in {start}..{end}'
	return np.array(times), np.stack(small), size


def _longest_run(flags: np.ndarray) -> tuple[int, int]:
	best, run_start, out = 0, None, (0, len(flags))
	for i, f in enumerate([*flags, False]):
		if f and run_start is None:
			run_start = i
		elif not f and run_start is not None:
			if i - run_start > best:
				best, out = i - run_start, (run_start, i)
			run_start = None
	return out


def _find_roi(small: np.ndarray) -> tuple[int, int, int, int]:
	"""The moving region (the player) in SMALL pixels, (x0, y0, x1, y1): the longest runs of rows and columns where
	pixels change over the span. Static page around the video is left out so it cannot dilute a cut's score."""
	gray = small.astype(np.float32).mean(-1)
	std = gray.std(0)
	moving = std > max(4.0, 0.25 * float(np.percentile(std, 99)))
	rows, cols = moving.mean(1), moving.mean(0)
	if rows.max() == 0:
		return 0, 0, small.shape[2], small.shape[1]
	y0, y1 = _longest_run(rows > 0.5 * rows.max())
	x0, x1 = _longest_run(cols > 0.5 * cols.max())
	return x0, y0, x1, y1


def _roi_to_source(roi: tuple[int, int, int, int], size: tuple[int, int]) -> dict:
	sx, sy = size[0] / SMALL[0], size[1] / SMALL[1]
	x0, y0, x1, y1 = roi
	return {'x': round(x0 * sx), 'y': round(y0 * sy), 'w': round((x1 - x0) * sx), 'h': round((y1 - y0) * sy)}


def _roi_from_source(roi: dict, size: tuple[int, int]) -> tuple[int, int, int, int]:
	sx, sy = size[0] / SMALL[0], size[1] / SMALL[1]
	x0, y0 = math.floor(roi['x'] / sx), math.floor(roi['y'] / sy)
	x1, y1 = math.ceil((roi['x'] + roi['w']) / sx), math.ceil((roi['y'] + roi['h']) / sy)
	return max(0, x0), max(0, y0), min(SMALL[0], x1), min(SMALL[1], y1)


def _parse_roi(text: str) -> dict:
	x, y, w, h = (int(v) for v in text.split(','))
	assert w > 0 and h > 0, f'--roi needs x,y,w,h with w,h > 0: {text}'
	return {'x': x, 'y': y, 'w': w, 'h': h}


# --- cuts --------------------------------------------------------------------------------------------------------------


def _scores(small: np.ndarray, roi: tuple[int, int, int, int]) -> tuple[np.ndarray, np.ndarray]:
	"""Per frame i >= 1, the change from frame i-1 inside the region: (luma grid distance, colour histogram distance),
	both 0..1. Luma: mean absolute difference of a 16x9 area-averaged grid / 255. Colour: total variation distance of
	64-bin RGB histograms (4 levels a channel). A cut moves both; motion inside a shot moves mostly the grid."""
	x0, y0, x1, y1 = roi
	grids, hists = [], []
	for frame in small[:, y0:y1, x0:x1]:
		image = Image.fromarray(np.ascontiguousarray(frame))
		grids.append(np.asarray(image.convert('L').resize(GRID, Image.Resampling.BOX), dtype=np.float32))
		q = frame // 64
		bins = np.bincount((q[..., 0] * 16 + q[..., 1] * 4 + q[..., 2]).ravel(), minlength=64).astype(np.float64)
		hists.append(bins / bins.sum())
	g, h = np.stack(grids), np.stack(hists)
	luma = np.concatenate([[0.0], np.abs(np.diff(g, axis=0)).mean((1, 2)) / 255])
	colour = np.concatenate([[0.0], 0.5 * np.abs(np.diff(h, axis=0)).sum(1)])
	return luma, colour


def detect_cuts(
	times: np.ndarray,
	luma: np.ndarray,
	colour: np.ndarray,
	min_score: float,
	ratio: float,
	window_s: float,
	min_gap_s: float,
) -> list[dict]:
	"""A cut at frame i (the first frame of the new shot) when its score s = (luma + colour) / 2:
	- is at least min_score, and at least `ratio` times the mean score of the frames within +-window_s (frame i left
	  out): the threshold adapts to how much the picture is already moving there;
	- is the highest within +-min_gap_s (one cut per change, the earliest on a tie)."""
	s = (luma + colour) / 2
	n = len(s)
	dt = float(np.median(np.diff(times))) if n > 1 else 1.0
	w, gap = max(1, round(window_s / dt)), max(1, round(min_gap_s / dt))
	out = []
	for i in range(1, n):
		if s[i] < min_score:
			continue
		lo, hi = max(1, i - gap), min(n, i + gap + 1)
		if s[i] < s[lo:hi].max() or any(s[j] == s[i] for j in range(lo, i)):
			continue
		near = [s[j] for j in range(max(1, i - w), min(n, i + w + 1)) if j != i]
		local = float(np.mean(near)) if near else 0.0
		if s[i] < ratio * local:
			continue
		last_change = next((j for j in range(i - 1, 0, -1) if s[j] >= STILL), 0)
		out.append(
			{
				'rec_s': round(float(times[i]), 3),
				'score': round(float(s[i]), 4),
				'ratio': round(float(s[i] / local), 1) if local > 0 else None,
				'luma': round(float(luma[i]), 4),
				'colour': round(float(colour[i]), 4),
				# frames unchanged right before the cut: the cut reached the recording up to this much earlier
				# (a frozen screencast, or simply a still shot)
				'unchanged_before_s': round(float(times[i - 1] - times[last_change]), 3),
			}
		)
	return out


def cmd_cuts(args: argparse.Namespace) -> None:
	started = time.monotonic()
	times, small, size = _scan(args.recording, args.start, args.end)
	if args.roi:
		roi_src, roi_from = _parse_roi(args.roi), 'given'
		roi = _roi_from_source(roi_src, size)
	else:
		roi = _find_roi(small)
		roi_src, roi_from = _roi_to_source(roi, size), 'auto (moving region)'
	luma, colour = _scores(small, roi)
	cuts = detect_cuts(times, luma, colour, args.min_score, args.ratio, args.window, args.min_gap)
	s = (luma + colour) / 2
	# runs of unchanged frames of 1 s or more: a frozen recording (the screencast stopped) or a perfectly still
	# picture; this file cannot tell which. A cut inside one shows only when it ends (see unchanged_before_s)
	stretches, run_start = [], None
	for i in range(1, len(s) + 1):
		still = i < len(s) and s[i] < STILL
		if still and run_start is None:
			run_start = i - 1
		elif not still and run_start is not None:
			if times[i - 1] - times[run_start] >= 1.0:
				stretches.append([round(float(times[run_start]), 3), round(float(times[i - 1]), 3)])
			run_start = None
	_emit(
		{
			'tool': 'ground_truth.py cuts',
			'script_version': _version(),
			'recording': str(Path(args.recording).resolve()),
			'recording_sha256': _sha256(args.recording),
			'frames': len(times),
			'fps': round(1 / float(np.median(np.diff(times))), 3) if len(times) > 1 else None,
			'span_s': [round(float(times[0]), 3), round(float(times[-1]), 3)],
			'source_size': list(size),
			'roi': roi_src,
			'roi_from': roi_from,
			'params': {
				'min_score': args.min_score,
				'ratio': args.ratio,
				'window_s': args.window,
				'min_gap_s': args.min_gap,
				'score': '(luma 16x9 grid MAD/255 + RGB 64-bin total variation) / 2, frame i vs i-1, inside roi',
			},
			'unchanged_frames_share': round(float((s[1:] < STILL).mean()), 3),
			'unchanged_stretches_s': stretches,
			'n_cuts': len(cuts),
			'cuts': cuts,
			'runtime_s': round(time.monotonic() - started, 1),
		},
		args.out,
	)


# --- align -------------------------------------------------------------------------------------------------------------


def _match(film: list[float], rec: list[float], offset: float, tol: float) -> list[tuple[float, float]]:
	"""Maximum one-to-one matching of edit cuts (film s, + offset) to detected cuts (rec s) within tol: on sorted lists
	the greedy two-pointer walk is optimal for interval matching."""
	pairs, i, j = [], 0, 0
	shifted = [c + offset for c in film]
	while i < len(shifted) and j < len(rec):
		if rec[j] < shifted[i] - tol:
			j += 1
		elif rec[j] > shifted[i] + tol:
			i += 1
		else:
			pairs.append((film[i], rec[j]))
			i += 1
			j += 1
	return pairs


def align(
	detections: list[dict],
	edits: list[float],
	span: tuple[float, float],
	film_span: tuple[float, float],
	max_offset: float,
	tol: float,
) -> dict:
	"""The offset (rec_s = film_s + offset) that matches the most edit cuts to detected cuts within tol, one to one.
	Every offset that puts some edit cut on some detection is a candidate; ties go to the smaller mean residual, then
	to the smaller |offset|. The winner is refined to its matches' median residual if that does not lose a match."""
	rec = sorted(d['rec_s'] for d in detections)
	unchanged = {d['rec_s']: d.get('unchanged_before_s') for d in detections}
	edits = sorted(c for c in edits if film_span[0] <= c <= film_span[1])

	def inside(offset: float) -> list[float]:
		return [c for c in edits if span[0] <= c + offset <= span[1]]

	def evaluate(offset: float) -> tuple[int, float, float]:
		pairs = _match(inside(offset), rec, offset, tol)
		mean_res = statistics.fmean(abs(r - (c + offset)) for c, r in pairs) if pairs else math.inf
		return len(pairs), mean_res, abs(offset)

	candidates = sorted({round(r - c, 4) for r in rec for c in edits if abs(r - c) <= max_offset})
	assert candidates, 'no candidate offset within --max-offset'
	scored = {o: evaluate(o) for o in candidates}
	best = min(candidates, key=lambda o: (-scored[o][0], scored[o][1], scored[o][2], o))
	pairs = _match(inside(best), rec, best, tol)
	refined = round(best + statistics.median(r - (c + best) for c, r in pairs), 4)
	if evaluate(refined)[0] >= scored[best][0] and evaluate(refined)[1] <= scored[best][1]:
		best = refined
	cuts_in = inside(best)
	pairs = _match(cuts_in, rec, best, tol)
	residuals = [round(r - (c + best), 3) for c, r in pairs]
	matched_film = {c for c, _ in pairs}
	matched_rec = {r for _, r in pairs}
	runner_up = max((o for o in candidates if abs(o - best) > 1.0), key=lambda o: (scored[o][0], -scored[o][1]), default=None)
	rate = len(pairs) / len(cuts_in) if cuts_in else 0.0
	# the looser view measure_watch.py takes of a reported cut (0.7 s): is there any detection near each edit cut
	near = [c for c in cuts_in if any(abs(r - (c + best)) <= 0.7 for r in rec)]
	thirds = [residuals[k * len(residuals) // 3 : (k + 1) * len(residuals) // 3] for k in range(3)]

	def nearest(c: float) -> dict:
		r = min(rec, key=lambda r: (abs(r - (c + best)), r))
		# a detection after a run of unchanged frames (a frozen recording, or a still shot) may show a cut late
		return {'delta_s': round(r - (c + best), 3), 'rec_s': r, 'unchanged_before_s': unchanged.get(r)}

	return {
		'verdict': 'ALIGNED' if rate >= 0.8 else 'NO ALIGNMENT',
		'offset_s': round(best, 3),
		'matched': len(pairs),
		'edit_cuts_in_span': len(cuts_in),
		'match_rate': round(rate, 3),
		'residual_s': {
			'median_abs': round(statistics.median(abs(x) for x in residuals), 3) if residuals else None,
			'max_abs': round(max(abs(x) for x in residuals), 3) if residuals else None,
			'mean_signed': round(statistics.fmean(residuals), 3) if residuals else None,
			# a steady offset keeps these alike; a stall or a drifting clock moves them apart
			'mean_signed_by_third': [round(statistics.fmean(t), 3) if t else None for t in thirds],
		},
		'edit_cuts_with_a_detection_within_0.7s': len(near),
		'runner_up': ({'offset_s': runner_up, 'matched': scored[runner_up][0]} if runner_up is not None else None),
		'unmatched_edit_cuts': [
			{'film_s': c, 'rec_s_expected': round(c + best, 3), 'nearest_detection': nearest(c)}
			for c in cuts_in
			if c not in matched_film
		],
		'unmatched_detections': [r for r in rec if r not in matched_rec],
		'pairs': [{'film_s': c, 'rec_s': r, 'residual_s': round(r - (c + best), 3)} for c, r in pairs],
	}


def cmd_align(args: argparse.Namespace) -> None:
	cuts = json.loads(Path(args.cuts).read_text())
	edits = json.loads(Path(args.edits).read_text())['cuts']
	span = (
		args.start if args.start is not None else cuts['span_s'][0],
		args.end if args.end is not None else cuts['span_s'][1],
	)
	detections = [c for c in cuts['cuts'] if span[0] <= c['rec_s'] <= span[1]]
	assert detections, f'no detected cuts in the recording span {span}'
	film_span = (args.film_from, args.film_to if args.film_to is not None else math.inf)
	result = align(detections, edits, span, film_span, args.max_offset, args.tol)
	_emit(
		{
			'tool': 'ground_truth.py align',
			'script_version': _version(),
			'cuts_file': str(Path(args.cuts).resolve()),
			'cuts_file_sha256': _sha256(args.cuts),
			'edits_file': str(Path(args.edits).resolve()),
			'edits_file_sha256': _sha256(args.edits),
			'recording': cuts.get('recording'),
			'rec_span_s': list(span),
			'film_span_s': [film_span[0], None if math.isinf(film_span[1]) else film_span[1]],
			'tolerance_s': args.tol,
			'max_offset_s': args.max_offset,
			'detections_in_span': len(detections),
			**result,
		},
		args.out,
	)


# --- sheets ------------------------------------------------------------------------------------------------------------


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
	try:
		return ImageFont.load_default(size=size)
	except TypeError:  # Pillow < 10.1 has one bitmap size
		return ImageFont.load_default()


def cmd_sheets(args: argparse.Namespace) -> None:
	started = time.monotonic()
	out = Path(args.out)
	out.mkdir(parents=True, exist_ok=True)
	cut_times: list[float] = []
	size, duration = _probe(args.recording)
	if args.cuts:
		cuts = json.loads(Path(args.cuts).read_text())
		assert Path(cuts['recording']).resolve() == Path(args.recording).resolve(), 'cuts.json is for another file'
		cut_times = [c['rec_s'] for c in cuts['cuts']]
	if args.roi == 'full':
		roi = {'x': 0, 'y': 0, 'w': size[0], 'h': size[1]}
	elif args.roi:
		roi = _parse_roi(args.roi)
	elif args.cuts:
		roi = cuts['roi']
	else:
		_, small, size = _scan(args.recording, args.start, args.end)
		x0, y0, x1, y1 = _find_roi(small)
		# a short span can leave a still edge of the player out of the moving region: keep one cell more each side
		padded = (max(0, x0 - 1), max(0, y0 - 1), min(SMALL[0], x1 + 1), min(SMALL[1], y1 + 1))
		roi = _roi_to_source(padded, size)
	start = args.start if args.start is not None else 0.0
	end = args.end if args.end is not None else duration
	steps = int(math.floor((end - start) / args.every + 1e-9)) + 1
	wanted = [round(start + k * args.every, 3) for k in range(steps)]
	# the frame on screen at t: the last frame whose presentation time is <= t
	shots: dict[float, Image.Image] = {}
	k, previous = 0, None
	x, y, w, h = roi['x'], roi['y'], roi['w'], roi['h']
	tile_w = args.tile_width
	tile_h = max(1, round(tile_w * h / w))

	def grab(frame: av.VideoFrame) -> Image.Image:
		return frame.to_image().crop((x, y, x + w, y + h)).resize((tile_w, tile_h), Image.Resampling.LANCZOS)

	for t, frame in _frames(args.recording, start, None):
		while k < len(wanted) and wanted[k] < t - 1e-6:
			if previous is not None:
				shots[wanted[k]] = grab(previous)
			k += 1
		if k >= len(wanted):
			break
		previous = frame
		if t > end + 1e-6:
			break
	while k < len(wanted) and previous is not None:
		shots[wanted[k]] = grab(previous)
		k += 1
	times = [t for t in wanted if t in shots]
	per = args.cols * args.rows
	bar, pad, head = 34, 8, 40
	label_font, head_font = _font(24), _font(22)
	name = Path(args.recording).name
	sheets = []
	for n, first in enumerate(range(0, len(times), per)):
		chunk = times[first : first + per]
		sheet = Image.new('RGB', (pad + args.cols * (tile_w + pad), head + args.rows * (tile_h + bar + pad)), (16, 16, 16))
		draw = ImageDraw.Draw(sheet)
		total = math.ceil(len(times) / per)
		draw.text(
			(pad, 8),
			f'{name}  sheet {n + 1}/{total}  rec {chunk[0]:.2f}-{chunk[-1]:.2f} s  every {args.every} s',
			fill=(230, 230, 230),
			font=head_font,
		)
		for j, t in enumerate(chunk):
			col, row = j % args.cols, j // args.cols
			left, top = pad + col * (tile_w + pad), head + row * (tile_h + bar + pad)
			label = f'{t:.2f} s'
			# a detected cut since the previous tile (objective, from this recording only): the labeller checks it
			between = [c for c in cut_times if t - args.every < c <= t + 1e-6]
			if between:
				label += '   detector cut ' + ', '.join(f'{c:.2f}' for c in between)
			draw.rectangle((left, top, left + tile_w, top + bar), fill=(40, 40, 40) if not between else (110, 30, 30))
			draw.text((left + 6, top + 4), label, fill=(255, 255, 255), font=label_font)
			sheet.paste(shots[t], (left, top + bar))
		file = f'sheet_{n + 1:03d}.jpg'
		sheet.save(out / file, quality=args.quality)
		sheets.append({'file': file, 'from_s': chunk[0], 'to_s': chunk[-1], 'tiles_s': chunk})
	index = {
		'tool': 'ground_truth.py sheets',
		'script_version': _version(),
		'recording': str(Path(args.recording).resolve()),
		'recording_sha256': _sha256(args.recording),
		'every_s': args.every,
		'span_s': [times[0], times[-1]] if times else None,
		'roi': roi,
		'grid': [args.cols, args.rows],
		'tile_size': [tile_w, tile_h],
		'cuts_file': str(Path(args.cuts).resolve()) if args.cuts else None,
		'tile_rule': 'each tile is the frame on screen at its stamped recording second (last frame with pts <= t)',
		'sheets': sheets,
		'runtime_s': round(time.monotonic() - started, 1),
	}
	(out / 'index.json').write_text(json.dumps(index, indent=1) + '\n')
	print(json.dumps({k: v for k, v in index.items() if k != 'sheets'} | {'n_sheets': len(sheets)}, indent=1))


# --- labels: template and freeze ---------------------------------------------------------------------------------------


def _not_placeholder(v: str) -> str:
	assert v.strip(), 'must not be empty'
	assert PLACEHOLDER not in v, f'still holds the template placeholder "{PLACEHOLDER}"'
	return v


Text = Annotated[str, AfterValidator(_not_placeholder)]


class Event(BaseModel):
	model_config = ConfigDict(extra='forbid')

	rec_s: float = Field(ge=0)
	film_s: float | None = None
	kind: Literal['cut', 'action', 'appear', 'disappear', 'text', 'sound']
	what: Text


class Labels(BaseModel):
	"""One labeller's events on one recording, made before any narration is scored."""

	model_config = ConfigDict(extra='forbid')

	stimulus: Text
	recording: Text
	offset_s: float | None  # rec_s = film_s + offset_s when align said ALIGNED, else null (labels by rec_s only)
	labeller: Text
	made_before_scoring: Literal[True]
	events: list[Event] = Field(min_length=1)
	# optional: the recording span the labeller covered, and the recording's sha256 (checked against the file)
	span_rec_s: tuple[float, float] | None = None
	recording_sha256: str | None = None

	@model_validator(mode='after')
	def _consistent(self) -> 'Labels':
		times = [e.rec_s for e in self.events]
		assert times == sorted(times), 'events must be in rec_s order'
		seen = set()
		for e in self.events:
			key = (e.rec_s, e.kind, e.what)
			assert key not in seen, f'duplicate event {key}'
			seen.add(key)
			if self.offset_s is None:
				assert e.film_s is None, f'film_s given at rec {e.rec_s} but offset_s is null (no alignment)'
			elif e.film_s is not None:
				assert abs(e.film_s - (e.rec_s - self.offset_s)) <= 0.05, (
					f'film_s {e.film_s} != rec_s {e.rec_s} - offset_s {self.offset_s} (+-0.05)'
				)
			if self.span_rec_s:
				assert self.span_rec_s[0] - 1e-6 <= e.rec_s <= self.span_rec_s[1] + 1e-6, (
					f'event at {e.rec_s} is outside span_rec_s {self.span_rec_s}'
				)
		return self


def cmd_template(args: argparse.Namespace) -> None:
	out = Path(args.out)
	assert not out.exists() or args.force, f'{out} exists (use --force to overwrite)'
	example = {
		'rec_s': 0.0,
		'film_s': None if args.offset is None else round(0.0 - args.offset, 3),
		'kind': 'cut',
		'what': f'{PLACEHOLDER}: one line per event, what is on screen (or heard), in rec_s order',
	}
	labels = {
		'stimulus': args.stimulus or f'{PLACEHOLDER}: film, source, span (e.g. Big Buck Bunny, film 0-190 s)',
		'recording': str(Path(args.recording).resolve()) if args.recording else f'{PLACEHOLDER}: recording path',
		'offset_s': args.offset,
		'labeller': args.labeller or f'{PLACEHOLDER}: agent id of the labeller',
		'made_before_scoring': True,
		'events': [example],
		'span_rec_s': None,
		'recording_sha256': _sha256(args.recording) if args.recording and Path(args.recording).exists() else None,
	}
	out.parent.mkdir(parents=True, exist_ok=True)
	out.write_text(json.dumps(labels, indent=1) + '\n')
	print(f'wrote {out}: kinds = cut | action | appear | disappear | text | sound; replace every "{PLACEHOLDER}"')


def cmd_freeze(args: argparse.Namespace) -> None:
	path = Path(args.labels)
	raw = path.read_bytes()
	try:
		labels = Labels.model_validate_json(raw)
	except ValidationError as e:
		print(f'NOT FROZEN: {path} is invalid\n{e}', file=sys.stderr)
		sys.exit(1)
	recording = Path(labels.recording)
	rec_hash = None
	if recording.exists():
		rec_hash = _sha256(recording)
		if labels.recording_sha256 and labels.recording_sha256 != rec_hash:
			print(f'NOT FROZEN: recording_sha256 does not match {recording}', file=sys.stderr)
			sys.exit(1)
		duration = _probe(recording)[1]
		late = [e.rec_s for e in labels.events if e.rec_s > duration + 0.5]
		if late:
			print(f'NOT FROZEN: events after the recording ends ({duration:.2f} s): {late}', file=sys.stderr)
			sys.exit(1)
	digest = hashlib.sha256(raw).hexdigest()
	if not args.keep_writable:
		os.chmod(path, os.stat(path).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
	kinds: dict[str, int] = {}
	for e in labels.events:
		kinds[e.kind] = kinds.get(e.kind, 0) + 1
	print(
		json.dumps(
			{
				'tool': 'ground_truth.py freeze',
				'script_version': _version(),
				'labels': str(path.resolve()),
				'labels_sha256': digest,
				'recording': str(recording),
				'recording_sha256': rec_hash if rec_hash else 'NOT FOUND (path not on this machine)',
				'labeller': labels.labeller,
				'offset_s': labels.offset_s,
				'events': len(labels.events),
				'by_kind': dict(sorted(kinds.items())),
				'rec_span_s': [labels.events[0].rec_s, labels.events[-1].rec_s],
				# a courtesy only: root ignores it. The sha256 logged before scoring is the guard
				'write_bits_cleared': not args.keep_writable,
			},
			indent=1,
		)
	)
	print(
		f'LOOP_MEMORY line: LABELS FROZEN {path.name} sha256 {digest} ({len(labels.events)} events, labeller '
		f'{labels.labeller}, recording {recording.name}), before any narration is scored.'
	)


# --- cli ---------------------------------------------------------------------------------------------------------------


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	sub = parser.add_subparsers(dest='command', required=True)

	p = sub.add_parser('cuts', help='objective scene cuts in recording seconds')
	p.add_argument('recording')
	p.add_argument('--from', dest='start', type=float)
	p.add_argument('--to', dest='end', type=float)
	p.add_argument('--out')
	p.add_argument('--roi', help='x,y,w,h in recording pixels (default: the moving region, found automatically)')
	p.add_argument('--min-score', type=float, default=0.12)
	# tuned once, on E2 (retinat-20261022-154838.mp4): 3.0 -> 5.0 dropped two in-shot actions (ratios 4.0, 4.1); the
	# weakest true cut there has 8.6. B1 (retinat-20261022-154352.mp4) was held out and run after the tune
	p.add_argument('--ratio', type=float, default=5.0, help='score / mean score of the frames around it')
	p.add_argument('--window', type=float, default=1.0, help='seconds each side for the adaptive threshold')
	p.add_argument('--min-gap', type=float, default=0.375, help='seconds: one cut per change')
	p.set_defaults(func=cmd_cuts)

	p = sub.add_parser('align', help='offset rec_s = film_s + offset from cuts vs the film edit list')
	p.add_argument('cuts')
	p.add_argument('--edits', required=True)
	p.add_argument('--max-offset', type=float, default=60.0)
	p.add_argument('--tol', type=float, default=0.2)
	p.add_argument('--from', dest='start', type=float, help='recording span (default: the cuts file span)')
	p.add_argument('--to', dest='end', type=float)
	p.add_argument('--film-from', type=float, default=0.0, help='film seconds the stimulus holds')
	p.add_argument('--film-to', type=float, help='e.g. 190 for protocol v2 (its file is the first 190 s)')
	p.add_argument('--out')
	p.set_defaults(func=cmd_align)

	p = sub.add_parser('sheets', help='contact sheets stamped with recording seconds, plus index.json')
	p.add_argument('recording')
	p.add_argument('--out', required=True)
	p.add_argument('--every', type=float, default=0.5)
	p.add_argument('--from', dest='start', type=float)
	p.add_argument('--to', dest='end', type=float)
	p.add_argument('--cuts', help='cuts.json of this recording: its region, and its cuts marked on the tiles')
	p.add_argument('--roi', help='x,y,w,h in recording pixels, or "full" (default: from --cuts, else found)')
	p.add_argument('--cols', type=int, default=4)
	p.add_argument('--rows', type=int, default=3)
	p.add_argument('--tile-width', type=int, default=480)
	p.add_argument('--quality', type=int, default=88)
	p.set_defaults(func=cmd_sheets)

	p = sub.add_parser('template', help='an empty label file for the labeller')
	p.add_argument('--out', required=True)
	p.add_argument('--recording')
	p.add_argument('--stimulus')
	p.add_argument('--offset', type=float, help='offset_s from align (only when it said ALIGNED)')
	p.add_argument('--labeller')
	p.add_argument('--force', action='store_true')
	p.set_defaults(func=cmd_template)

	p = sub.add_parser('freeze', help='validate a label file and print its sha256')
	p.add_argument('labels')
	p.add_argument('--keep-writable', action='store_true', help='do not make the file read-only')
	p.set_defaults(func=cmd_freeze)

	args = parser.parse_args()
	args.func(args)


if __name__ == '__main__':
	main()
