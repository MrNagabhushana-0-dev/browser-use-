"""eyesbench: does the information an agent needs on a dynamic page ever reach it, and at what cost?

Every browser agent from the big labs perceives a page as a screenshot per step (Antigravity, Claude in Chrome,
OpenAI's computer use) or an accessibility snapshot (Playwright MCP). Dynamic content falls between those steps:
a code shown for 400 ms, a sound, an animation. This benchmark measures that directly, without an LLM judge:
each task is seeded (so answers cannot be memorised), its ground truth is known, and each perception mode is
scored on whether the needed information was **captured** at all, whether it is **in what the model is sent**,
and the **estimated tokens** that cost. It is a necessary-condition test: information an agent never received
cannot be answered, however good the model is.

Modes:
- `screenshots`: one screenshot every `period` seconds (default 1.5 s, a fast agent step), as the screenshot-loop
  agents see the page.
- `retina`: `Eyes.watch` for the same span: the video's decoded frames and audio, summarised as one percept.

Run `python -m browser_use.eyes.bench` for a table. Media is generated locally with ffmpeg; nothing is fetched.
"""

from __future__ import annotations

import asyncio
import base64
import io
import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SHOT_WIDTH = 1280
FLASH_COLOURS = {'magenta': (255, 0, 255), 'cyan': (0, 255, 255), 'yellow': (255, 255, 0), 'lime': (0, 255, 0)}
PAGE = (
	'<!doctype html><title>{title}</title><body style="margin:0;background:#000">'
	'<video src="{src}" autoplay muted playsinline style="height:100vh;display:block;margin:auto"></video></body>'
)


@dataclass
class Task:
	name: str
	seed: int
	question: str
	seconds: float  # how long a perceiver gets
	media: bytes = field(repr=False)
	truth: dict[str, Any] = field(default_factory=dict)

	def page(self, src: str) -> str:
		return PAGE.format(title=self.name, src=src)


def _ffmpeg() -> str:
	import imageio_ffmpeg

	return imageio_ffmpeg.get_ffmpeg_exe()


def _render(args: list[str], out: Path) -> bytes:
	subprocess.run([_ffmpeg(), '-y', '-loglevel', 'error', *args, str(out)], check=True)
	return out.read_bytes()


def flash_task(seed: int, work: Path) -> Task:
	"""A 15 s dark video in which the whole frame flashes one colour for 0.4 s at a seeded moment."""
	rng = random.Random(seed)
	colour = rng.choice(sorted(FLASH_COLOURS))
	at = round(rng.uniform(3.0, 11.0), 2)
	r, g, b = FLASH_COLOURS[colour]
	media = _render(
		[
			*['-f', 'lavfi', '-i', 'color=c=0x101418:s=360x640:r=30:d=15'],
			*['-f', 'lavfi', '-i', f'color=c=0x{r:02x}{g:02x}{b:02x}:s=360x640:r=30:d=15'],
			*['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=15'],
			*['-filter_complex', f"[0][1]overlay=enable='between(t,{at},{at + 0.4})',format=yuv420p[v]"],
			*['-map', '[v]', '-map', '2:a', '-c:v', 'libvpx-vp9', '-b:v', '200k', '-deadline', 'realtime'],
			*['-cpu-used', '8', '-c:a', 'libopus', '-shortest'],
		],
		work / f'flash-{seed}.webm',
	)
	return Task('flash', seed, 'Which colour flashed, briefly, during the video?', 14.0, media, {'colour': colour, 'at': at})


def beeps_task(seed: int, work: Path) -> Task:
	"""A 12 s muted video whose audio has N short beeps at seeded times (at least 0.8 s apart)."""
	rng = random.Random(seed)
	n = rng.randint(3, 7)
	times: list[float] = []
	while len(times) < n:
		t = round(rng.uniform(1.0, 10.5), 2)
		if all(abs(t - x) >= 0.8 for x in times):
			times.append(t)
	times.sort()
	gate = '+'.join(f'between(t,{t},{t + 0.12})' for t in times)
	media = _render(
		[
			*['-f', 'lavfi', '-i', 'color=c=0x203040:s=360x640:r=30:d=12'],
			*['-f', 'lavfi', '-i', f"aevalsrc='0.6*sin(2*PI*1000*t)*({gate})':s=48000:d=12"],
			*['-map', '0:v', '-map', '1:a', '-c:v', 'libvpx-vp9', '-b:v', '100k', '-deadline', 'realtime'],
			*['-cpu-used', '8', '-c:a', 'libopus', '-b:a', '96k'],
		],
		work / f'beeps-{seed}.webm',
	)
	return Task('beeps', seed, 'How many beeps are heard (the video is muted)?', 11.5, media, {'count': n, 'times': times})


def media_response(request, data: bytes, content_type: str = 'video/webm'):
	"""Serve media with HTTP byte ranges, as real servers do: Chrome cannot seek a video without them."""
	import re

	from werkzeug import Response

	headers = {'Accept-Ranges': 'bytes', 'Content-Type': content_type}
	match = re.match(r'bytes=(\d+)-(\d*)', request.headers.get('Range', ''))
	if not match:
		return Response(data, headers=headers)
	start = int(match.group(1))
	end = min(int(match.group(2)) if match.group(2) else len(data) - 1, len(data) - 1)
	headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
	return Response(data[start : end + 1], status=206, headers=headers)


def _mean_rgb(jpeg: bytes) -> tuple[int, int, int]:
	from PIL import Image

	with Image.open(io.BytesIO(jpeg)) as img:
		return img.convert('RGB').resize((1, 1)).getpixel((0, 0))  # type: ignore[return-value]


def _shows_colour(jpeg: bytes, colour: str, centre_only: bool = False) -> bool:
	"""Whether an image is dominated by the flash colour (the video is centred, so a screenshot's middle)."""
	from PIL import Image

	with Image.open(io.BytesIO(jpeg)) as img:
		img = img.convert('RGB')
		if centre_only:
			w, h = img.size
			img = img.crop((w * 0.42, h * 0.3, w * 0.58, h * 0.7))
		r, g, b = img.resize((1, 1)).getpixel((0, 0))  # type: ignore[misc]
	tr, tg, tb = FLASH_COLOURS[colour]
	return abs(r - tr) < 70 and abs(g - tg) < 70 and abs(b - tb) < 70


async def screenshot_loop(session, seconds: float, period: float = 1.5) -> tuple[list[bytes], int]:
	"""Screenshots every `period` s for `seconds`, as a screenshot-per-step agent sees the page; and their tokens."""
	from browser_use.eyes.percept import estimate_image_tokens

	cdp = await session.get_or_create_cdp_session(focus=False)
	shots: list[bytes] = []
	tokens = 0
	loop = asyncio.get_event_loop()
	end = loop.time() + seconds
	metrics = await cdp.cdp_client.send.Page.getLayoutMetrics(session_id=cdp.session_id)
	vw, vh = metrics['cssLayoutViewport']['clientWidth'], metrics['cssLayoutViewport']['clientHeight']
	scale = min(1.0, SHOT_WIDTH / vw)  # the screenshot agents downscale (OpenAI: 1440x900, Anthropic: 1280x800)
	clip = {'x': 0, 'y': 0, 'width': vw, 'height': vh, 'scale': scale}
	while loop.time() < end:
		r = await cdp.cdp_client.send.Page.captureScreenshot(
			params={'format': 'jpeg', 'quality': 70, 'clip': clip}, session_id=cdp.session_id  # type: ignore[typeddict-item]
		)
		jpeg = base64.b64decode(r['data'])
		shots.append(jpeg)
		from PIL import Image

		with Image.open(io.BytesIO(jpeg)) as img:
			tokens += estimate_image_tokens(*img.size)
		await asyncio.sleep(period)
	return shots, tokens


def score_screenshots(task: Task, shots: list[bytes]) -> dict[str, Any]:
	if task.name == 'flash':
		seen = any(_shows_colour(s, task.truth['colour'], centre_only=True) for s in shots)
		return {'captured': seen, 'sent': seen, 'answer': task.truth['colour'] if seen else None}
	# Screenshots carry no sound.
	return {'captured': False, 'sent': False, 'answer': None}


def score_retina(task: Task, percept) -> dict[str, Any]:
	item = percept.items[0] if percept.items else None
	if item is None:
		return {'captured': False, 'sent': False, 'answer': None}
	if task.name == 'flash':
		a, colour = task.truth['at'], task.truth['colour']
		tr, tg, tb = FLASH_COLOURS[colour]
		captured = any(
			a - 0.05 <= f.t <= a + 0.45 and abs(f.rgb[0] - tr) < 70 and abs(f.rgb[1] - tg) < 70 and abs(f.rgb[2] - tb) < 70
			for f in item.frames
		)
		sent = any(k.jpeg and _shows_colour(k.jpeg, colour) for k in item.keyframes) or colour in percept.text
		return {'captured': captured, 'sent': sent, 'answer': colour if sent else None}
	onsets = [t for t in item.hearing.onsets if 0.5 <= t <= 11.5]
	count = len(onsets)
	return {'captured': count == task.truth['count'], 'sent': f'{count} onsets' in percept.text, 'answer': count}


async def run(session, eyes, base_url: str, serve, seeds: tuple[int, ...] = (1, 2, 3), work: Path | None = None) -> list[dict]:
	"""Run every task for each seed in both modes. `serve(path, page_html, media_path, media_bytes)` hosts a task."""
	import tempfile

	work = work or Path(tempfile.mkdtemp(prefix='eyesbench_'))
	rows: list[dict] = []
	for seed in seeds:
		for make in (flash_task, beeps_task):
			task = make(seed, work)
			media_path = f'/{task.name}-{seed}.webm'
			for mode in ('screenshots', 'retina'):
				page_path = f'/{task.name}-{seed}-{mode}'
				serve(page_path, task.page(media_path), media_path, task.media)
				await session.navigate_to(base_url + page_path)
				if mode == 'screenshots':
					await asyncio.sleep(0.5)
					shots, tokens = await screenshot_loop(session, task.seconds)
					score = score_screenshots(task, shots)
					observations = len(shots)
				else:
					await eyes.open()
					percept = await eyes.watch(seconds=task.seconds, until='time')
					score = score_retina(task, percept)
					tokens, observations = percept.tokens, 1
				correct = score['answer'] == (task.truth.get('colour') if task.name == 'flash' else task.truth['count'])
				rows.append(
					{'task': task.name, 'seed': seed, 'mode': mode, 'observations': observations, 'tokens': tokens}
					| score
					| {'correct': bool(correct)}
				)
	return rows


def table(rows: list[dict]) -> str:
	lines = ['task    seed  mode         captured  sent   correct  observations  ~tokens']
	for r in rows:
		lines.append(
			f'{r["task"]:<7} {r["seed"]:<5} {r["mode"]:<12} {str(r["captured"]):<9} {str(r["sent"]):<6} '
			f'{str(r["correct"]):<8} {r["observations"]:<13} {r["tokens"]}'
		)
	return '\n'.join(lines)


async def _main() -> None:
	from pytest_httpserver import HTTPServer

	from browser_use.browser import BrowserProfile, BrowserSession
	from browser_use.eyes import Eyes

	server = HTTPServer()
	server.start()

	def serve(page_path: str, html: str, media_path: str, media: bytes) -> None:
		server.expect_request(page_path).respond_with_data(html, content_type='text/html')
		server.expect_request(media_path).respond_with_handler(lambda r: media_response(r, media))

	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=False, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await session.start()
	eyes = Eyes(session, speech=False, now_path=False)
	try:
		rows = await run(session, eyes, server.url_for('').rstrip('/'), serve)
		print(table(rows))
	finally:
		await eyes.close()
		await session.kill()
		server.stop()


if __name__ == '__main__':
	asyncio.run(_main())
