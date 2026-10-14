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
from browser_use.eyes.views import Deadline
from browser_use.vision.live import LiveView

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession
	from browser_use.human.input import HumanInput

GRID = 16
# How long one read of the page may take before the scan gives up on it
EVAL_TIMEOUT_S = 10.0


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
	# Why it stopped: 'end' (the bottom), 'stuck' (the page would not scroll on), 'max_screens', 'time' (its deadline)
	# or 'unresponsive' (a read of the page did not come back)
	stopped: str = 'end'
	start_y: float = 0.0  # where it began (CSS px down the page)
	reached_y: float = 0.0  # the scroll position it last read
	next_y: float | None = None  # where a scan that goes on should begin, when there is more to see

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

	async def _eval(self, expression: str, timeout: float = EVAL_TIMEOUT_S):
		"""Evaluate in the page; TimeoutError if no answer within `timeout` (a CDP call has no time limit of its own,
		and a page that has stopped answering would hold the scan, and the call, forever)."""
		cdp = await self.session.get_or_create_cdp_session(focus=False)
		r = await asyncio.wait_for(
			cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
			),
			max(0.05, timeout),
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

	async def scan(
		self,
		max_screens: int = 25,
		dwell_s: float = 0.6,
		keyframes: int = 6,
		from_screen: float = 0.0,
		deadline: Deadline | None = None,
	) -> PageScan:
		"""Scroll down like a reader from `from_screen` screens down, watching; keep the frames that cover what was seen.

		With a `deadline`, stop in time to return what was seen: the result says why it stopped and where to go on
		(`next_y`). Without one it scrolls until the end, `max_screens`, or a page that will not scroll."""
		assert from_screen >= 0, 'from_screen is a number of screens down the page'
		dl = deadline or Deadline.unbounded()
		await self.start()
		assert self.live is not None
		live = self.live
		started = time.monotonic()
		# Only what is drawn from here on: the stream also holds frames of what the tab showed before (another page,
		# an earlier look), and a scan of a grey page showed the bands of the page before it
		last_at = live.clock()
		captured: list[PageFrame] = []
		samples: list[FrameSample] = []
		fixed_steps: list[list[list]] = []
		w, h = 1280.0, 800.0
		start_y = reached_y = 0.0
		height = 0
		screens = 0
		stopped = 'max_screens'

		def take(frame: PageFrame) -> None:
			# Signatures as the frames come in, so the work after the loop does not grow with the frames seen
			captured.append(frame)
			luma, rgb, grid4 = signature(frame.jpeg)
			samples.append(FrameSample(len(samples) + 1, 1, frame.at, frame.at, luma, rgb, True, grid4))

		async def collect() -> tuple[float, bool, int]:
			"""Take in the frames drawn since the last call; (where the page is scrolled, at its end, its height)."""
			nonlocal last_at
			state = await self._eval(
				f'[scrollY, scrollY + innerHeight >= document.documentElement.scrollHeight - 4,'
				f' document.documentElement.scrollHeight, {FIXED_JS}]',
				timeout=dl.clamp(EVAL_TIMEOUT_S),
			) or [0, True, 0, []]
			y = float(state[0] or 0)
			fixed_steps.append(state[3] or [])
			fresh = [f for f in live.frames if f.at > last_at]
			if fresh:
				last_at = fresh[-1].at
			for f in fresh:
				if f.scroll_y is not None and f.scroll_y < start_y - 2:
					continue  # drawn before the jump to where this scan begins, and acked after it
				# Where Chrome drew the frame, not where the page is now: frames from mid-scroll were labelled with
				# where it ended up (found on the Guardian: two frames "6.4 screens down" with different stories)
				take(PageFrame(time.monotonic() - started, f.scroll_y if f.scroll_y is not None else y, f.data))
			if not any(abs(c.scroll_y - y) <= 2 for c in captured[-8:]):
				# Nothing drawn from here: a still page that the scan has not moved yet, or a stream that stopped
				# under the scan (a recording took it over, the explorer stopped its view on this tab). A picture of
				# it as it is, so what the scan says it saw is what it saw
				shot = await asyncio.wait_for(self.capture(), max(0.05, dl.clamp(5.0)))
				if shot:
					take(PageFrame(time.monotonic() - started, y, shot))
			return y, bool(state[1]), int(state[2] or 0)

		try:
			# Few reads of the page, each doing what it can: on a page drawing a frame a second (WebGL in software)
			# every read waits ~2 s for the page, and a one-screen scan took eight of them
			t = time.monotonic()
			w, h, start_y = await self._eval(
				f"window.scrollTo({{top: {from_screen} * innerHeight, behavior: 'instant'}}), [innerWidth, innerHeight, scrollY]",
				timeout=dl.clamp(EVAL_TIMEOUT_S),
			) or [1280, 800, 0]
			w, h, start_y = float(w), float(h), float(start_y)
			probe = time.monotonic() - t
			reached_y = start_y
			# What a step takes, grown to the longest one seen; and what must still fit after the loop
			step_est = dwell_s + 3 * probe + 0.3
			tail_est = min(dwell_s, 0.3) + 2 * probe + 0.5
			if dl.allows(0.4 + tail_est):
				await asyncio.sleep(0.4)
				await self.hand.move_to(w * 0.55, h * 0.5)
			wheeled = False
			for _ in range(max_screens):
				if not dl.allows(step_est + tail_est + 0.002 * len(captured)):
					stopped = 'time'
					break
				t = time.monotonic()
				await asyncio.sleep(dwell_s)  # read, and let anything that animates on arrival play
				reached_y, at_end, height = await collect()
				wheeled = False
				if at_end:
					stopped = 'end'
					break
				await self.hand.wheel(h * 0.8)
				wheeled = True
				screens += 1
				if await self._eval('scrollY', timeout=dl.clamp(EVAL_TIMEOUT_S)) == reached_y:
					if not dl.allows(0.3 + probe + tail_est):
						stopped = 'time'
						break
					await asyncio.sleep(0.3)
					if await self._eval('scrollY', timeout=dl.clamp(EVAL_TIMEOUT_S)) == reached_y:
						stopped = 'stuck'
						break
				step_est = max(step_est, time.monotonic() - t)
			if wheeled:
				# What the last turn of the wheel brought into view
				await asyncio.sleep(dl.clamp(dwell_s, reserve=tail_est))
				reached_y, at_end, height = await collect()
				if at_end and stopped == 'max_screens':
					stopped = 'end'
		except TimeoutError:
			stopped = 'time' if not dl.allows(0.5) else 'unresponsive'
		try:
			await self._eval("window.scrollTo({top: 0, behavior: 'instant'})", timeout=max(0.5, dl.clamp(2.0)))
		except TimeoutError:
			pass

		height = max(height, int(h))
		indices, shown = choose_page_frames(captured, samples, keyframes, float(h), float(height))
		# Where the picture changed while the page was not being scrolled: things moving on their own.
		moving: list[float] = []
		deltas = sight.deltas(sight.grids(samples), sight.colour_grids(samples))
		for i in range(1, len(captured)):
			a, b = captured[i - 1], captured[i]
			if a.scroll_y == b.scroll_y and deltas[i] > 6 and (not moving or abs(moving[-1] - a.scroll_y) > h * 0.5):
				moving.append(a.scroll_y)
		more = stopped in ('time', 'max_screens') and reached_y + h < height - 4
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
			stopped=stopped,
			start_y=start_y,
			reached_y=reached_y,
			next_y=reached_y + h * 0.8 if more else None,
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
