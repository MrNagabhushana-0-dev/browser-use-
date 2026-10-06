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
- `retina`: `Eyes.watch` for the same span: the video's decoded frames and audio, summarised as one percept. On a
  page whose answer is text (toast, ticker): the journal of text that appeared, plus one page look at the end.

Tasks: a colour flash, beeps, a toast, bounces on a canvas, and a live value that crosses its alert line for one
250 ms tick (`ticker`). `ticker-static` holds that value on screen: every mode must read it, so a miss on the live
page is the sampling, not a blind scorer.

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
	html: str | None = field(default=None, repr=False)  # a page of its own (no media), else a video player

	def page(self, src: str) -> str:
		return self.html if self.html is not None else PAGE.format(title=self.name, src=src)

	@property
	def answer(self) -> Any:
		keys = {'flash': 'colour', 'beeps': 'count', 'toast': 'id', 'bounce': 'count', 'ticker': 'peak', 'ticker-static': 'peak'}
		return self.truth[keys[self.name]]

	@property
	def needle(self) -> str | None:
		"""The text that carries the answer on a page whose answer is text (toast, ticker), else None."""
		if self.name == 'toast':
			return str(self.truth['id'])
		if self.name.startswith('ticker'):
			return f'{self.truth["peak"]}%'
		return None


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


TOAST_RGB = (255, 122, 0)
TOAST_PAGE = """<!doctype html><title>orders</title><body style="margin:0;font:16px sans-serif;background:#f4f4f4">
<main style="padding:40px"><h1>Your orders</h1><p>Recent activity appears here.</p></main>
<script>setTimeout(() => {{ const t = document.createElement('div');
t.setAttribute('role', 'status'); t.textContent = 'Order #{id} confirmed';
t.style.cssText = 'position:fixed;left:50%;bottom:60px;transform:translateX(-50%);background:#ff7a00;color:#000;'
  + 'padding:28px 48px;font:bold 28px sans-serif;border-radius:12px';
document.body.appendChild(t); setTimeout(() => t.remove(), {dur_ms}); }}, {at_ms});</script></body>"""


def toast_task(seed: int, work: Path) -> Task:
	"""A page that shows an order-ID toast for 1.5 s at a seeded moment, then removes it."""
	rng = random.Random(seed)
	order_id = rng.randint(10000, 99999)
	at = round(rng.uniform(2.0, 8.0), 2)
	html = TOAST_PAGE.format(id=order_id, at_ms=int(at * 1000), dur_ms=1500)
	return Task('toast', seed, 'What order ID was confirmed?', 10.0, b'', {'id': order_id, 'at': at}, html=html)


TICKER_ALERT_RGB = (220, 30, 30)
TICKER_PAGE = """<!doctype html><title>{title}</title><body style="margin:0;font:16px sans-serif;background:#f4f4f4">
<main style="padding:40px"><h1>Cluster load</h1><p>Live, updated four times a second. Alert at 90%.</p>
<div id="v" role="status" style="display:inline-block;padding:28px 48px;font:bold 48px sans-serif;border-radius:12px;
background:#2a7;color:#fff">Load {first}%</div></main>
<script>const values = {values}; const box = document.getElementById('v'); let i = 0;
const show = (x) => {{ box.textContent = 'Load ' + x + '%'; box.style.background = x >= 90 ? '#dc1e1e' : '#2a7'; }};
if (values.length > 1) setInterval(() => {{ i = Math.min(i + 1, values.length - 1); show(values[i]); }}, 250);
show(values[0]);</script></body>"""


def _ticker_values(seed: int) -> tuple[list[int], int, float]:
	rng = random.Random(seed * 53 + 11)
	values = [rng.randint(40, 85) for _ in range(40)]  # 10 s at 4 a second, all under the alert line
	k = rng.randint(8, 36)
	values[k] = rng.randint(91, 99)  # the one tick over it
	return values, values[k], k * 0.25


def ticker_task(seed: int, work: Path) -> Task:
	"""A dashboard value that updates every 250 ms and crosses its alert line once, for one tick."""
	values, peak, at = _ticker_values(seed)
	html = TICKER_PAGE.format(title='ticker', first=values[0], values=values)
	return Task('ticker', seed, 'What was the highest load shown?', 10.5, b'', {'peak': peak, 'at': at}, html=html)


def ticker_static_task(seed: int, work: Path) -> Task:
	"""The ticker's twin: the same peak, held on screen. Every mode must read it, or a scorer is blind."""
	values, peak, at = _ticker_values(seed)
	html = TICKER_PAGE.format(title='ticker-static', first=peak, values=[peak])
	return Task('ticker-static', seed, 'What was the highest load shown?', 3.5, b'', {'peak': peak, 'at': 0.0}, html=html)


BOUNCE_PAGE = """<!doctype html><title>bounce</title><body style="margin:0;background:#000">
<canvas width="360" height="640" style="height:100vh;display:block;margin:auto"></canvas>
<script>const hits = {hits}; const c = document.querySelector('canvas'); const g = c.getContext('2d');
const R = 30, floor = 640 - R - 10; let t0 = null;
const y = (t) => {{
  if (t < hits[0]) {{ const s = t / hits[0]; return floor - (floor - 60) * (1 - s * s); }}
  for (let k = 0; k + 1 < hits.length; k++) if (t < hits[k + 1]) {{
    const gap = hits[k + 1] - hits[k], s = (t - hits[k]) / gap, H = Math.min(480, 120 + 100 * gap);
    return floor - H * 4 * s * (1 - s); }}
  return floor; }};
const draw = (now) => {{ if (t0 === null) t0 = now; const t = (now - t0) / 1000;
  g.fillStyle = '#101418'; g.fillRect(0, 0, 360, 640);
  g.fillStyle = '#f0f0f0'; g.beginPath(); g.arc(180 + 60 * Math.sin(t * 0.7), y(t), R, 0, 7); g.fill();
  requestAnimationFrame(draw); }};
requestAnimationFrame(draw);</script></body>"""


def bounce_task(seed: int, work: Path) -> Task:
	"""A ball on a <canvas> (no video element, nothing in the DOM) hits the floor N times at seeded moments."""
	rng = random.Random(seed * 31 + 7)
	n = rng.randint(3, 7)
	hits: list[float] = []
	while len(hits) < n:
		t = round(rng.uniform(1.0, 10.5), 2)
		if all(abs(t - x) >= 0.8 for x in hits):
			hits.append(t)
	hits.sort()
	html = BOUNCE_PAGE.format(hits=hits)
	return Task('bounce', seed, 'How many times does the ball hit the floor?', 12.0, b'', {'count': n, 'hits': hits}, html=html)


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
			params={'format': 'jpeg', 'quality': 70, 'clip': clip},
			session_id=cdp.session_id,  # type: ignore[typeddict-item]
		)
		jpeg = base64.b64decode(r['data'])
		shots.append(jpeg)
		from PIL import Image

		with Image.open(io.BytesIO(jpeg)) as img:
			tokens += estimate_image_tokens(*img.size)
		await asyncio.sleep(period)
	return shots, tokens


async def snapshot_loop(session, seconds: float, period: float = 1.5) -> tuple[list[str], int]:
	"""Accessibility snapshots every `period` s, as Playwright-MCP-style agents read a page, and their tokens.

	Serialised compactly (one `role "name"` line per named node, like Playwright's snapshot), so the cost is
	not inflated by raw JSON.
	"""
	cdp = await session.get_or_create_cdp_session(focus=False)
	snaps: list[str] = []
	loop = asyncio.get_event_loop()
	end = loop.time() + seconds
	while loop.time() < end:
		tree = await cdp.cdp_client.send.Accessibility.getFullAXTree(session_id=cdp.session_id)
		lines = []
		for node in tree.get('nodes', []):
			if node.get('ignored'):
				continue
			name = str((node.get('name') or {}).get('value') or '').strip()
			role = str((node.get('role') or {}).get('value') or '')
			if name:
				lines.append(f'- {role} "{name}"')
		snaps.append('\n'.join(lines))
		await asyncio.sleep(period)
	return snaps, sum(len(x) for x in snaps) // 4


def _shows_toast(jpeg: bytes) -> bool:
	from PIL import Image

	with Image.open(io.BytesIO(jpeg)) as img:
		small = img.convert('RGB').resize((160, 90))
		hits = sum(1 for r, g, b in small.getdata() if abs(r - 255) < 40 and abs(g - 122) < 40 and b < 60)  # type: ignore[misc]
	return hits >= 40  # the toast is ~3% of the screen; a stray orange pixel is not it


def _shows_alert(jpeg: bytes) -> bool:
	from PIL import Image

	tr, tg, tb = TICKER_ALERT_RGB
	with Image.open(io.BytesIO(jpeg)) as img:
		small = img.convert('RGB').resize((160, 90))
		hits = sum(1 for r, g, b in small.getdata() if abs(r - tr) < 40 and abs(g - tg) < 40 and abs(b - tb) < 40)  # type: ignore[misc]
	return hits >= 40  # the alert box is a few percent of the screen


def score_snapshots(task: Task, snaps: list[str]) -> dict[str, Any]:
	if task.needle:
		seen = any(task.needle in s for s in snaps)
		return {'captured': seen, 'sent': seen, 'answer': task.answer if seen else None}
	# A video's or a canvas's pixels and sound are not in the accessibility tree.
	return {'captured': False, 'sent': False, 'answer': None}


def score_screenshots(task: Task, shots: list[bytes]) -> dict[str, Any]:
	if task.name == 'flash':
		seen = any(_shows_colour(s, task.truth['colour'], centre_only=True) for s in shots)
		return {'captured': seen, 'sent': seen, 'answer': task.truth['colour'] if seen else None}
	if task.name == 'toast':  # if a shot caught the toast, assume the model can read its large text
		seen = any(_shows_toast(s) for s in shots)
		return {'captured': seen, 'sent': seen, 'answer': task.truth['id'] if seen else None}
	if task.name.startswith('ticker'):  # likewise: a shot that caught the red alert box shows the peak in 48px text
		seen = any(_shows_alert(s) for s in shots)
		return {'captured': seen, 'sent': seen, 'answer': task.answer if seen else None}
	# Screenshots carry no sound, and a count of bounces is not in any one frame: scored as not captured,
	# which flatters nothing (a model would have to infer hits from a few ball positions).
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
	if task.name == 'bounce':
		hits = item.motion.bottom if item.motion else []
		count = len(hits)
		return {'captured': count == task.truth['count'], 'sent': f'the bottom {count} times' in percept.text, 'answer': count}
	onsets = [t for t in item.hearing.onsets if 0.5 <= t <= 11.5]
	count = len(onsets)
	return {'captured': count == task.truth['count'], 'sent': f'{count} onsets' in percept.text, 'answer': count}


MODES = ('screenshots', 'snapshots', 'retina')


async def _retina_text(session, task: Task, work: Path) -> tuple[dict[str, Any], int]:
	"""Retina on a page with no media: what the journal (delivered by the hook each turn) says appeared, plus one
	look at the page at the end, as an agent would take. The journal reports text that appears; what was on the page
	from the start is in the look, not in the journal."""
	import json

	from browser_use.eyes import Eyes

	assert task.needle, f'{task.name} has no text answer'
	now_path = work / f'{task.name}-{task.seed}' / 'now.json'
	eyes = Eyes(session, speech=False, now_path=now_path, archive=False)
	await eyes.open()
	try:
		await asyncio.sleep(task.seconds)
		await eyes.retina.wait_for_data(1.5)
		captured = any(task.needle in str(e.data.get('text', '')) for e in eyes.retina.events if e.type == 'text')
		look = await eyes.look()
	finally:
		await eyes.close()
	journal = eyes.journal_path.read_text() if eyes.journal_path and eyes.journal_path.exists() else ''
	texts = [json.loads(line).get('text', '') for line in journal.splitlines()]
	in_look = bool(look.image) and _shows_answer(task, look.image)
	sent = any(task.needle in t for t in texts) or in_look
	tokens = sum(len(t) for t in texts) // 4 + look.tokens
	return {'captured': captured or in_look, 'sent': sent, 'answer': task.answer if sent else None}, tokens


def _shows_answer(task: Task, jpeg: bytes) -> bool:
	"""Whether an image of the page shows the answer, by the same rule the screenshot scorer uses."""
	return _shows_toast(jpeg) if task.name == 'toast' else _shows_alert(jpeg)


async def run(
	session,
	eyes,
	base_url: str,
	serve,
	seeds: tuple[int, ...] = (1, 2, 3),
	work: Path | None = None,
	tasks: tuple = (),
	modes: tuple[str, ...] = MODES,
	period: float = 1.5,
) -> list[dict]:
	"""Run every task for each seed in each mode. `serve(path, page_html, media_path, media_bytes)` hosts a task.

	`period` is the loop modes' step: 1.5 s is a fast agent; measured real agents take 5-15 s a step.
	"""
	import tempfile

	work = work or Path(tempfile.mkdtemp(prefix='eyesbench_'))
	rows: list[dict] = []
	for seed in seeds:
		for make in tasks or (flash_task, beeps_task, toast_task):
			task = make(seed, work)
			media_path = f'/{task.name}-{seed}.webm'
			for mode in modes:
				page_path = f'/{task.name}-{seed}-{mode}'
				serve(page_path, task.page(media_path), media_path, task.media)
				if mode == 'retina' and not task.needle:
					await eyes.open()  # before the page loads, as retinat_open does: the first moments count
				await session.navigate_to(base_url + page_path)
				if mode == 'screenshots':
					await asyncio.sleep(0.5)
					shots, tokens = await screenshot_loop(session, task.seconds, period)
					score, observations = score_screenshots(task, shots), len(shots)
				elif mode == 'snapshots':
					await asyncio.sleep(0.5)
					snaps, tokens = await snapshot_loop(session, task.seconds, period)
					score, observations = score_snapshots(task, snaps), len(snaps)
				elif task.needle:
					score, tokens = await _retina_text(session, task, work)
					observations = 1
				else:
					percept = await eyes.watch(seconds=task.seconds, until='time')
					score = score_retina(task, percept)
					tokens, observations = percept.tokens, 1
				rows.append(
					{
						'task': task.name,
						'seed': seed,
						'mode': mode,
						'period': period,
						'observations': observations,
						'tokens': tokens,
					}
					| score
					| {'correct': score['answer'] == task.answer}
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
