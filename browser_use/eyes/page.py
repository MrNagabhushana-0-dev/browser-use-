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
import io
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
	coverage: float
	screens: int
	seconds: float
	page_height: int
	viewport: tuple[int, int]
	moving: list[float] = field(default_factory=list)  # scroll positions where the picture moved on its own

	@property
	def cuts(self) -> list[float]:
		return sight.read(self.samples).cuts


class PageWatcher:
	"""Watch the rendered page through the compositor's frame stream."""

	def __init__(self, browser_session: 'BrowserSession', hand: 'HumanInput', max_width: int = 640, quality: int = 60) -> None:
		self.session = browser_session
		self.hand = hand
		self.max_width = max_width
		self.quality = quality
		self.live: LiveView | None = None

	async def start(self) -> None:
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
		frames = self.live.frames if self.live else []
		return frames[-1].data if frames else None

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

		async def collect() -> None:
			nonlocal seen
			frames = self.live.frames if self.live else []
			y = float(await self._eval('scrollY') or 0)
			for f in frames[seen:]:
				captured.append(PageFrame(time.monotonic() - started, y, f.data))
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
		selection = sight.select_keyframes(samples, keyframes)
		# Where the picture changed while the page was not being scrolled: things moving on their own.
		moving: list[float] = []
		deltas = sight.deltas(sight.grids(samples), sight.colour_grids(samples))
		for i in range(1, len(captured)):
			a, b = captured[i - 1], captured[i]
			if a.scroll_y == b.scroll_y and deltas[i] > 6 and (not moving or abs(moving[-1] - a.scroll_y) > h * 0.5):
				moving.append(a.scroll_y)
		return PageScan(
			frames=captured,
			samples=samples,
			keyframes=[captured[i] for i in selection.indices],
			coverage=selection.coverage,
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
