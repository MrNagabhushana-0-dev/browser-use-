"""Replay a change signal through the 4-state gate and its single-threshold ablation: the flapping-vs-clipping pair.

Sources of signal (each gives (wall_s, score) at 10 frames a second, scored as the retina scores frames: sight.deltas
over a 16x16 luma grid and a 4x4 colour grid):
  video FILE [--to S]     decode a film and score it
  log FILE                a live run's gate log (eyes/gate.py GateLog 'signal' lines)
  borderline [--events]   a synthetic UI page: a caret blinking at 1 Hz, a sparkline creeping 1 px a frame, a clock
                          ticking each second; --events adds three real changes (a dialog opening at 20 s, a chart
                          redrawn at 40 s, a toast at 50 s) whose onsets are known
Reports, per gate: transitions per hour (flapping), episodes, and onset clipping for known event onsets (seconds of
an event's start before the gate went ACTIVE, which a gate without a reservoir would never commit; with the reservoir
the commit starts at the cursor, before the onset, so 0). The rendering and the replay are deterministic.

usage: python scripts/contract/gate_replay.py video FILE [--to 190] [--edits edits.json]
       python scripts/contract/gate_replay.py borderline [--seconds 60] [--events]
       python scripts/contract/gate_replay.py log FILE
"""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from browser_use.eyes import sight
from browser_use.eyes.gate import Gate

FPS = 10.0


def _grids(img: Image.Image) -> tuple[np.ndarray, np.ndarray]:
	luma = np.asarray(img.convert('L').resize((16, 16), Image.Resampling.BOX), dtype=np.float32).reshape(-1)
	colours = np.asarray(img.convert('RGB').resize((4, 4), Image.Resampling.BOX), dtype=np.float32).reshape(-1)
	return luma, colours


def _scores(images) -> list[tuple[float, float]]:
	lumas, colours, walls = [], [], []
	for wall, img in images:
		a, b = _grids(img)
		lumas.append(a)
		colours.append(b)
		walls.append(wall)
	d = sight.deltas(np.stack(lumas), np.stack(colours))
	return list(zip(walls, d.tolist()))


def from_video(path: Path, to: float) -> list[tuple[float, float]]:
	import av

	def frames():
		with av.open(str(path)) as c:
			stream = c.streams.video[0]
			assert stream.time_base is not None
			nxt = 0.0
			for f in c.decode(stream):
				if f.pts is None:
					continue
				t = float(f.pts * stream.time_base)
				if t > to:
					return
				if t + 1e-6 >= nxt:
					nxt += 1 / FPS
					yield t, f.to_image()

	return _scores(frames())


def borderline(seconds: float, events: bool, pulse: float = 0.0) -> tuple[list[tuple[float, float]], list[float]]:
	"""A dashboard page as a 640x360 render: what a person leaves open while an agent watches."""
	rng = np.random.default_rng(7)
	base = Image.new('RGB', (640, 360), (245, 246, 248))
	d = ImageDraw.Draw(base)
	for i in range(6):  # static cards
		d.rectangle(
			[20 + (i % 3) * 205, 60 + (i // 3) * 140, 205 + (i % 3) * 205, 180 + (i // 3) * 140],
			fill=(255, 255, 255),
			outline=(220, 222, 226),
		)
	d.rectangle([0, 0, 640, 40], fill=(40, 44, 52))
	spark = (rng.normal(0, 1, 2000).cumsum() * 3).astype(int)
	onsets = [20.0, 40.0, 50.0] if events else []

	def render(t: float) -> Image.Image:
		img = base.copy()
		g = ImageDraw.Draw(img)
		n = int(t * FPS)
		pts = [(30 + x, 120 - int(spark[(n + x) % 2000]) // 4) for x in range(170)]  # creeps 1 px a frame
		g.line(pts, fill=(60, 120, 220), width=1)
		g.text((560, 14), f'12:{int(t) // 60:02d}:{int(t) % 60:02d}', fill=(230, 230, 230))  # a clock ticking each second
		if pulse:  # an animated card whose change hovers at the gate's threshold (a live tile, a spinner)
			v = int(200 + pulse * np.sin(2 * np.pi * 0.7 * t) + rng.normal(0, pulse / 3))
			g.rectangle([430, 200, 610, 320], fill=(v, v, min(255, v + 20)))
		if int(t * 2) % 2 == 0:
			g.rectangle([250, 225, 251, 243], fill=(20, 20, 20))  # the caret, 500 ms on / 500 ms off
		if events and 20.0 <= t < 26.0:
			g.rectangle([160, 80, 480, 280], fill=(255, 255, 255), outline=(90, 90, 90))  # a dialog
			g.rectangle([160, 80, 480, 110], fill=(30, 110, 200))
		if events and t >= 40.0:
			g.rectangle([225, 200, 410, 320], fill=(250, 235, 210))  # a chart redrawn
		if events and 50.0 <= t < 53.0:
			g.rectangle([400, 300, 630, 350], fill=(40, 160, 90))  # a toast
		arr = np.asarray(img, dtype=np.int16) + rng.integers(-2, 3, (360, 640, 3))  # encoder noise
		return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

	return _scores((n / FPS, render(n / FPS)) for n in range(int(seconds * FPS))), onsets


def from_log(path: Path) -> list[tuple[float, float]]:
	return [(e['wall'], e['score']) for e in map(json.loads, open(path)) if e.get('kind') == 'signal']


def replay(signal: list[tuple[float, float]], onsets: list[float]) -> dict:
	out = {}
	for name, gate in (('schmitt_4state', Gate()), ('single_threshold', Gate.single_threshold())):
		for wall, score in signal:
			gate.feed(wall, score)
		# onset clipping: an event that begins while the gate is open loses nothing; one that begins while it is closed
		# loses what plays until the gate opens, unless a reservoir commits from before the onset
		opened = {'ACTIVE', 'HANGOVER'}
		clip = []
		for v in onsets:
			before = [to for wall, _, to in gate.transitions if wall <= v]
			if before and before[-1] in opened:
				clip.append(0.0)
				continue
			after = [e.active for e in gate.episodes if e.active >= v]
			clip.append(round(after[0] - v, 2) if after else None)
		out[name] = {
			**gate.stats(),
			'onset_clipping_without_reservoir_s': clip,
			'onset_clipping_with_reservoir_s': [0.0 if c is not None else None for c in clip],
		}
	return out


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	sub = parser.add_subparsers(dest='source', required=True)
	v = sub.add_parser('video')
	v.add_argument('file', type=Path)
	v.add_argument('--to', type=float, default=190.0)
	v.add_argument('--edits', type=Path)
	b = sub.add_parser('borderline')
	b.add_argument('--seconds', type=float, default=60.0)
	b.add_argument('--events', action='store_true')
	b.add_argument('--pulse', type=float, nargs='*', default=[0.0], help='animated-card amplitudes to sweep (grey levels)')
	lg = sub.add_parser('log')
	lg.add_argument('file', type=Path)
	args = parser.parse_args()
	if args.source == 'video':
		sig = from_video(args.file, args.to)
		cuts = [c for c in json.load(open(args.edits))['cuts'] if c <= args.to] if args.edits else []
		print(json.dumps({'source': str(args.file), 'seconds': args.to, **replay(sig, cuts)}, indent=1))
	elif args.source == 'borderline':
		rows = []
		for amp in args.pulse:
			sig, onsets = borderline(args.seconds, args.events, amp)
			scores = [s for _, s in sig]
			rows.append(
				{
					'pulse': amp,
					'score_median': round(float(np.median(scores)), 2),
					'score_p90': round(float(np.percentile(scores, 90)), 2),
					'events': onsets,
					**replay(sig, onsets),
				}
			)
		print(json.dumps({'source': 'borderline', 'seconds': args.seconds, 'rows': rows}, indent=1))
	else:
		print(json.dumps({'source': str(args.file), **replay(from_log(args.file), [])}, indent=1))
