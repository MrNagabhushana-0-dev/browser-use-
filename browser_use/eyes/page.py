"""Seeing a whole page, not just its videos: canvas, WebGL, CSS animation, the lot.

The retina taps `<video>` elements. A page drawn on `<canvas>` (a WebGL hero, a scroll-driven
scene, a chart) has no video to tap, and its markup says nothing about what it looks like. What a
person sees is the compositor's output, so that is what this watches: Chrome's screencast stream
(frames pushed by the compositor as they change, never a screenshot call), reduced to the same
16x16 luma signature the retina uses, so shots, change and keyframe selection all work the same.

`scan()` scrolls the page top to bottom with real wheel input while watching, then keeps the few
frames that cover everything it saw (greedy facility location, as for video). One sheet shows the
whole page as it looked while scrolling, including anything that animates as you go.
"""

import asyncio
import base64
import io
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from browser_use.browser.capture import capture_screenshot
from browser_use.eyes import sight
from browser_use.eyes.retina import FrameSample
from browser_use.vision.live import LiveView

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession
	from browser_use.human.input import HumanInput

GRID = 16


def signature(jpeg: bytes) -> tuple[bytes, tuple[int, int, int], bytes]:
	"""(16x16 luma, mean rgb, 4x4 rgb grid) of a JPEG frame, as the retina computes for video."""
	from PIL import Image

	img = Image.open(io.BytesIO(jpeg)).convert('RGB')
	small = img.resize((GRID, GRID), Image.Resampling.BILINEAR)
	luma = small.convert('L').tobytes()
	mean = tuple(int(c) for c in small.resize((1, 1), Image.Resampling.BILINEAR).getpixel((0, 0)))  # type: ignore[arg-type]
	grid4 = small.resize((4, 4), Image.Resampling.BILINEAR).tobytes()
	return luma, mean, grid4  # type: ignore[return-value]


@dataclass
class PageFrame:
	at: float  # seconds since the watch began
	scroll_y: float
	jpeg: bytes = field(repr=False)


@dataclass
class PageScan:
	frames: list[PageFrame]
	samples: list[FrameSample]
	keyframes: list[PageFrame]
	coverage: float  # share of the page's height the keyframes show
	screens: int
	seconds: float
	page_height: int
	viewport: tuple[int, int]
	moving: list[float] = field(default_factory=list)  # scroll positions where the picture moved on its own
	# Bands of the screen (top, bottom as fractions of its height) that stayed the same while the page scrolled under
	# them: a sticky header, a banner, a cookie bar. What is under them was not seen.
	fixed: list[tuple[float, float, str]] = field(default_factory=list)

	@property
	def cuts(self) -> list[float]:
		return sight.read(self.samples).cuts


# Elements fixed on screen (position fixed or sticky) across most of its width and at least this much of its height,
# asked of the page at each step of a scan: a band seen at half the steps or more stayed put while the page scrolled
FIXED_JS = """(() => {
  const vh = innerHeight, vw = innerWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if ((cs.position !== 'fixed' && cs.position !== 'sticky') || cs.visibility === 'hidden' || cs.display === 'none'
      || +cs.opacity < 0.1) continue;
    const r = el.getBoundingClientRect();
    const top = Math.max(0, r.top), bottom = Math.min(vh, r.bottom);
    const width = Math.min(vw, r.right) - Math.max(0, r.left);
    if (bottom - top < vh * 0.05 || width < vw * 0.5) continue;
    out.push([top / vh, bottom / vh, (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60)]);
  }
  return out;
})()"""


def choose_page_frames(
	frames: list[PageFrame], samples: list[FrameSample], k: int, viewport_h: float, page_h: float
) -> tuple[list[int], float]:
	"""The frames of a scan that show the most of the page, then what changed on its own; and the share of the page's
	height they show.

	A scan knows where each frame was scrolled, so frames are first chosen for the page height they add, the way a
	reader would page through it (found on BBC News: chosen by how alike they look, as for a film, two frames 0.1
	screens apart were both kept while strips of the page were on none; white pages of headlines all look alike at
	16x16). Of two frames that add as much, the later is kept: lazy images have had longer to load. Frames left over
	go to what differs most from those chosen, which is what moves on its own (a canvas, an animation)."""
	if not frames or k <= 0 or viewport_h <= 0:
		return [], 0.0
	page_h = max(page_h, max(f.scroll_y for f in frames) + viewport_h)
	chosen: list[int] = []
	spans: list[tuple[float, float]] = []

	def added(i: int) -> float:
		lo, hi = frames[i].scroll_y, min(frames[i].scroll_y + viewport_h, page_h)
		covered = 0.0
		for a, b in sorted(spans):
			covered += max(0.0, min(b, hi) - max(a, lo))
		return (hi - lo) - covered  # spans never overlap each other (each is trimmed to what it adds)

	while len(chosen) < k:
		gains = [(added(i), i) for i in range(len(frames)) if i not in chosen]
		if not gains:
			break
		gain, best = max(gains)  # ties: the later frame
		if gain < 0.02 * viewport_h:
			break
		chosen.append(best)
		lo, hi = frames[best].scroll_y, min(frames[best].scroll_y + viewport_h, page_h)
		for a, b in sorted(spans):  # keep only the new part, so spans stay disjoint
			if a <= lo < b:
				lo = b
			if a < hi <= b:
				hi = a
		if hi > lo:
			spans.append((lo, hi))
	shown = sum(b - a for a, b in spans) / page_h
	if len(chosen) < k and len(samples) == len(frames):
		matrix = sight.grids(samples)
		while len(chosen) < k:
			# how unlike anything chosen each frame is, among frames of the same stretch of page
			near = [
				i
				for i in range(len(frames))
				if i not in chosen and any(abs(frames[i].scroll_y - frames[j].scroll_y) < 0.1 * viewport_h for j in chosen)
			]
			if not near:
				break
			sim = sight.similarity(matrix[near], matrix[chosen])  # (near, chosen): small, unlike all frames by all
			novel = [(1.0 - float(sim[n].max()), i) for n, i in enumerate(near)]
			score, best = max(novel)
			if score < 0.05:
				break
			chosen.append(best)
	return sorted(chosen), min(1.0, shown)


def fixed_bands(steps: list[list[list]]) -> list[tuple[float, float, str]]:
	"""Bands (top, bottom as fractions of the screen, the text in them) that the page reported fixed at half or more of
	the scan's steps, overlapping ones merged."""
	seen: dict[tuple[float, float], tuple[int, str]] = {}
	for step in steps:
		merged: list[list] = []
		for top, bottom, text in sorted(step):
			if merged and top <= merged[-1][1] + 0.01:
				merged[-1][1] = max(merged[-1][1], bottom)
				merged[-1][2] = merged[-1][2] or text
			else:
				merged.append([top, bottom, text])
		for top, bottom, text in merged:
			key = (round(top, 2), round(bottom, 2))
			count, first = seen.get(key, (0, text))
			seen[key] = (count + 1, first or text)
	need = max(1, (len(steps) + 1) // 2)
	return [(top, bottom, text) for (top, bottom), (count, text) in sorted(seen.items()) if count >= need]


class PageWatcher:
	"""Watch the rendered page through the compositor's frame stream."""

	def __init__(self, browser_session: 'BrowserSession', hand: 'HumanInput', max_width: int = 640, quality: int = 60) -> None:
		self.session = browser_session
		self.hand = hand
		self.max_width = max_width
		self.quality = quality
		self.live: LiveView | None = None
		# The viewport (CSS px) at the last capture: a look image is smaller, and clicks take CSS px
		self.viewport: tuple[float, float] | None = None

	async def start(self) -> None:
		if self.live is not None and not self.live.in_step():
			await self.stop()  # a recording started or stopped since this view began: begin again in step with it
		if self.live is None:
			self.live = LiveView(self.session)
			await self.live.start(max_width=self.max_width, quality=self.quality)

	async def stop(self) -> None:
		if self.live is not None:
			try:
				await self.live.stop()
			finally:
				self.live = None

	async def _eval(self, expression: str):
		cdp = await self.session.get_or_create_cdp_session(focus=False)
		r = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
		)
		return (r.get('result') or {}).get('value')

	def latest(self) -> bytes | None:
		data = self.live.newest() if self.live else None
		return self.live.fit(data) if self.live and data else None

	async def capture(self) -> bytes | None:
		"""The viewport as the compositor draws it now, as a JPEG at this watcher's width, or None if that failed.

		A look takes this, not the stream's newest frame: the screencast is not 'now', and it drops the repaint that
		ends an animation (the recorder's top-up exists for that). That a stream read by reflex rules went stale as
		well was never shown: the test that seemed to show it had a stop rule that fired mid-animation."""
		try:
			cdp = await self.session.get_or_create_cdp_session(focus=False)
			metrics = await cdp.cdp_client.send.Page.getLayoutMetrics(session_id=cdp.session_id)
			vp = metrics['cssLayoutViewport']
			width, height = vp['clientWidth'], vp['clientHeight']
			self.viewport = (float(width), float(height))
			clip = {
				'x': vp['pageX'],
				'y': vp['pageY'],
				'width': width,
				'height': height,
				'scale': min(1.0, self.max_width / max(1, width)),
			}
			shot = await capture_screenshot(
				cdp.cdp_client, {'format': 'jpeg', 'quality': self.quality, 'clip': clip}, cdp.session_id
			)
			return base64.b64decode(shot['data'])
		except Exception:
			return None

	async def wait_latest(self, timeout: float = 4.0, settle: float = 0.3) -> bytes | None:
		"""The newest frame, waiting up to `timeout` for the first one rather than a fixed nap.

		A busy machine can take well over half a second to deliver the first screencast frame;
		`settle` still lets a page that is painting send a fresher one before we pick.
		"""
		await self.start()
		loop = asyncio.get_event_loop()
		started = loop.time()
		while loop.time() - started < timeout:
			if self.latest() and loop.time() - started >= settle:
				break
			await asyncio.sleep(0.05)
		return self.latest()

	async def scan(self, max_screens: int = 25, dwell_s: float = 0.6, keyframes: int = 6) -> PageScan:
		"""Scroll top to bottom like a reader, watching; keep the frames that cover what was seen."""
		await self.start()
		assert self.live is not None
		started = time.monotonic()
		w, h = await self._eval('[innerWidth, innerHeight]') or [1280, 800]
		await self._eval('window.scrollTo(0, 0)')
		await asyncio.sleep(0.4)
		await self.hand.move_to(w * 0.55, h * 0.5)
		captured: list[PageFrame] = []
		seen = 0

		fixed_steps: list[list[list]] = []

		async def collect() -> None:
			nonlocal seen
			frames = self.live.frames if self.live else []
			y = float(await self._eval('scrollY') or 0)
			fixed_steps.append(await self._eval(FIXED_JS) or [])
			for f in frames[seen:]:
				# Where Chrome drew the frame, not where the page is now: frames from mid-scroll were labelled with
				# where it ended up (found on the Guardian: two frames "6.4 screens down" with different stories)
				captured.append(PageFrame(time.monotonic() - started, f.scroll_y if f.scroll_y is not None else y, f.data))
			seen = len(frames)

		screens = 0
		for _ in range(max_screens):
			await asyncio.sleep(dwell_s)  # read, and let anything that animates on arrival play
			await collect()
			at_end = await self._eval('scrollY + innerHeight >= document.documentElement.scrollHeight - 4')
			if at_end:
				break
			before = await self._eval('scrollY')
			await self.hand.wheel(h * 0.8)
			screens += 1
			if await self._eval('scrollY') == before:
				await asyncio.sleep(0.3)
				if await self._eval('scrollY') == before:
					break
		await asyncio.sleep(dwell_s)
		await collect()
		height = int(await self._eval('document.documentElement.scrollHeight') or 0)
		await self._eval('window.scrollTo(0, 0)')

		samples: list[FrameSample] = []
		for i, frame in enumerate(captured):
			luma, rgb, grid4 = signature(frame.jpeg)
			samples.append(FrameSample(i + 1, 1, frame.at, frame.at, luma, rgb, True, grid4))
		height = max(height, int(h))
		indices, shown = choose_page_frames(captured, samples, keyframes, float(h), float(height))
		# Where the picture changed while the page was not being scrolled: things moving on their own.
		moving: list[float] = []
		deltas = sight.deltas(sight.grids(samples), sight.colour_grids(samples))
		for i in range(1, len(captured)):
			a, b = captured[i - 1], captured[i]
			if a.scroll_y == b.scroll_y and deltas[i] > 6 and (not moving or abs(moving[-1] - a.scroll_y) > h * 0.5):
				moving.append(a.scroll_y)
		return PageScan(
			fixed=fixed_bands(fixed_steps),
			frames=captured,
			samples=samples,
			keyframes=[captured[i] for i in indices],
			coverage=shown,
			screens=screens,
			seconds=time.monotonic() - started,
			page_height=height,
			viewport=(int(w), int(h)),
			moving=moving,
		)


def scan_sheet(scan: PageScan, tile_width: int = 300, columns: int = 4) -> tuple[bytes, int, int] | None:
	"""Keyframes of a scan in reading order, labelled with how far down the page each was."""
	if not scan.keyframes:
		return None
	from PIL import Image, ImageDraw, ImageFont

	tiles = []
	for kf in scan.keyframes:
		img = Image.open(io.BytesIO(kf.jpeg)).convert('RGB')
		img = img.resize((tile_width, max(1, round(img.height * tile_width / img.width))), Image.Resampling.LANCZOS)
		tiles.append((kf, img))
	th = max(t.height for _, t in tiles)
	rows = (len(tiles) + columns - 1) // columns
	cols = min(columns, len(tiles))
	sheet = Image.new('RGB', (cols * (tile_width + 4), rows * (th + 4)), (14, 14, 14))
	draw = ImageDraw.Draw(sheet)
	try:
		font = ImageFont.load_default(size=13)
	except TypeError:
		font = ImageFont.load_default()
	vh = max(1, scan.viewport[1])
	for i, (kf, img) in enumerate(tiles):
		x, y = (i % columns) * (tile_width + 4), (i // columns) * (th + 4)
		sheet.paste(img, (x, y))
		label = f'{i + 1}  {kf.scroll_y / vh:.1f} screens down'
		box = draw.textbbox((x + 4, y + 3), label, font=font)
		draw.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill=(0, 0, 0))
		draw.text((x + 4, y + 3), label, fill=(255, 255, 255), font=font)
	buf = io.BytesIO()
	sheet.save(buf, 'JPEG', quality=80)
	return buf.getvalue(), sheet.width, sheet.height
