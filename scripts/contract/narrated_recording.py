"""Burn a run's live narration into a copy of its screen recording, at the moment each line was written (proof that
narration came per result, not batched at the end).

The recording's first frame is taken as the wall time its 'record start' entry was logged. Each narration shows in a
band under the picture from the second it was written until the next one, with the result it answers and how long
after that result it came.

usage: python scripts/contract/narrated_recording.py <recording.mp4> <narration.jsonl> [--out out.mp4]
"""

import argparse
import json
import textwrap
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFont

BAND = 150
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def burn(recording: Path, log: Path, out: Path) -> dict:
	entries = [json.loads(line) for line in open(log) if line.strip()]
	starts = [e for e in entries if e['kind'] == 'record' and e['action'] == 'start' and Path(e['path']).name == recording.name]
	assert starts, f'no record start for {recording.name} in {log}'
	t0 = starts[-1]['wall']
	lines = sorted((e['wall'] - t0, e) for e in entries if e['kind'] == 'narration' and e['wall'] >= t0)
	font = ImageFont.truetype(FONT, 17)
	small = ImageFont.truetype(FONT, 14)
	src = av.open(str(recording))
	vin = src.streams.video[0]
	dst = av.open(str(out), 'w')
	rate = vin.average_rate or 8
	vout = dst.add_stream('libx264', rate=rate)
	vout.width, vout.height = vin.codec_context.width, vin.codec_context.height + BAND
	vout.width -= vout.width % 2
	vout.height -= vout.height % 2
	vout.pix_fmt = 'yuv420p'
	shown = 0
	for frame in src.decode(vin):
		t = float(frame.pts * vin.time_base) if frame.pts is not None else 0.0
		img = frame.to_image()
		canvas = Image.new('RGB', (vout.width, vout.height), (0, 0, 0))
		canvas.paste(img, (0, 0))
		draw = ImageDraw.Draw(canvas)
		now = [e for at, e in lines if at <= t]
		if now:
			e = now[-1]
			after = f', {e["wall"] - e["returned"]:.1f}s after it' if e.get('returned') else ''
			head = f'narration of result {e["n"]} (covering {e["covering"]}) written at rec {e["wall"] - t0:.1f}s{after}'
			draw.text((10, img.height + 6), head, fill=(255, 210, 80), font=small)
			body = textwrap.wrap(' / '.join(e['text'].splitlines()), width=max(40, vout.width // 10))[:5]
			for i, row in enumerate(body):
				draw.text((10, img.height + 28 + i * 22), row, fill=(255, 255, 255), font=font)
			shown = max(shown, len(now))
		else:
			draw.text((10, img.height + 6), 'no narration yet', fill=(160, 160, 160), font=small)
		draw.text((vout.width - 120, img.height + 6), f'rec {t:6.1f}s', fill=(160, 160, 160), font=small)
		packet = vout.encode(av.VideoFrame.from_image(canvas).reformat(format='yuv420p'))
		dst.mux(packet)
	dst.mux(vout.encode())
	dst.close()
	src.close()
	return {'out': str(out), 'narrations': len(lines), 'shown': shown, 'first_at_s': round(lines[0][0], 1) if lines else None}


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('recording', type=Path)
	parser.add_argument('log', type=Path)
	parser.add_argument('--out', type=Path)
	args = parser.parse_args()
	out = args.out or args.recording.with_suffix('.narrated.mp4')
	print(json.dumps(burn(args.recording, args.log, out), indent=1))
