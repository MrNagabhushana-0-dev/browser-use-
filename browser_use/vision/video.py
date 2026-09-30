"""Watch a video from its pixels, and pay for the moments that matter instead of the minutes.

A transcript says what was spoken. It says nothing about what was shown: a chart, a demo, a
diagram, a face, a product. Looking at the video is the only way to know, and the obvious way
to look — screenshot the player once a second — costs one full image per second for a
stream in which most seconds repeat the one before. A ten minute video is six hundred images.

This does not play the video. It seeks it, which makes the watch independent of playback
speed, of buffering stalls, and of whether the tab is in the foreground, and it lets the
sampling be *adaptive*:

1. Take a coarse grid of samples across the whole duration.
2. Wherever two neighbouring samples differ, halve the interval and look at the middle, and
   keep halving the side that still differs until it is narrower than `min_gap`. A cut is
   located in about log2(interval/min_gap) extra samples, and stretches where nothing
   changes cost nothing beyond the grid.
3. Do that for the biggest changes first and stop once the caller's frame budget is spent. A
   ten minute film may have hundreds of cuts; locating all of them to keep eight would spend
   most of the samples on cuts that are then thrown away.
4. The span between two cuts is a shot. Take one keyframe from the middle of each.

What makes the search cheap is that a *sample* is not an image. Each one is an 8x8 colour
thumbnail computed inside the page from the video element itself, a couple of hundred bytes
that never cross the wire as pixels. Only the final keyframes are ever screenshotted, and
they are then laid out on one contact sheet with timestamps burned in, because what a model
is charged for is image *area* and not image count, so one sheet of a dozen keyframes costs
a fraction of a dozen images.

The browser refuses to let a page read back pixels from a cross-origin video (the canvas is
"tainted"). That is detected on the first sample, and the watcher then compares screenshots
of the player instead. It is slower and heavier, and it is labelled as such in the result,
but it finds the same cuts.

Limits, stated rather than discovered: a shot shorter than the coarse grid step can fall
between two samples and be missed, which is what `coarse` trades against cost; a fade is
reported as one cut where it changes fastest; and a video inside an iframe, or one behind
DRM, is not reachable from here.
"""

import base64
import heapq
import json
import logging
import math
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any

from browser_use.vision.live import comparison_available, frame_signature, signature_distance

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

# Thumbnail difference, 0-100, above which two samples count as different shots. Solid
# scene changes score 30+; a small object crossing a scene scores under 3. The live-view
# threshold of 4 is tuned for "did anything move", which is the wrong question here.
CUT_THRESHOLD = 10

# Largest keyframe width captured, in CSS pixels. Keyframes are resized again for the
# contact sheet, so capturing more only spends bandwidth.
KEYFRAME_MAX_WIDTH = 640

# How close to the end of the video the last sample is taken. Seeking exactly to the
# duration lands on no frame in some browsers.
_END_MARGIN = 0.05

# Longest a single seek may take before the watch is abandoned. Generous because a first
# seek into a remote video may have to fetch a segment first.
DEFAULT_SEEK_TIMEOUT = 15.0

# Published image pricing is by area, with large images scaled down first.
_TOKEN_AREA_DIVISOR = 750
_MAX_LONG_EDGE = 1568
_MAX_PIXELS = 1_150_000


class NoVideoError(RuntimeError):
	"""The page has no visible video element to watch, or it has no picture."""


class _TaintedMidWatch(Exception):
	"""Pixels stopped being readable after some in-page signatures were already taken."""


def estimate_image_tokens(width: int, height: int) -> int:
	"""Approximate token cost of one image, by the published area rule.

	An estimate and named as one: width*height/750 after scaling oversized images down. It is
	accurate to within a few percent for the sizes that matter here, which is enough to
	compare strategies and not enough to reconcile an invoice.
	"""
	assert width > 0 and height > 0, 'image dimensions must be positive'
	scale = min(1.0, _MAX_LONG_EDGE / max(width, height), math.sqrt(_MAX_PIXELS / (width * height)))
	return round(width * scale * height * scale / _TOKEN_AREA_DIVISOR)


@dataclass
class TokenLedger:
	"""What was sent to the model, with the cost of the obvious alternative alongside.

	Everything here is an estimate from sizes. The real figure for a model call comes back in
	its usage object and belongs to the token tracker; this exists so a strategy can be
	judged before, and without, a model call.
	"""

	entries: list[tuple[str, str, int]] = field(default_factory=list)  # (kind, label, tokens)

	def add_image(self, width: int, height: int, label: str) -> int:
		tokens = estimate_image_tokens(width, height)
		self.entries.append(('image', label, tokens))
		return tokens

	def add_text(self, text: str, label: str) -> int:
		tokens = max(1, round(len(text) / 4))
		self.entries.append(('text', label, tokens))
		return tokens

	@property
	def total(self) -> int:
		return sum(tokens for _, _, tokens in self.entries)

	@property
	def total_image_tokens(self) -> int:
		return sum(tokens for kind, _, tokens in self.entries if kind == 'image')

	@staticmethod
	def naive_screenshot_tokens(seconds: float, width: int, height: int, every: float = 1.0) -> int:
		"""Cost of screenshotting the whole frame every `every` seconds for `seconds`."""
		return math.ceil(seconds / every) * estimate_image_tokens(width, height)

	def describe(self) -> str:
		parts = [f'{label}: ~{tokens} tok' for _, label, tokens in self.entries]
		return f'~{self.total} tokens ({"; ".join(parts)})' if parts else '0 tokens'


@dataclass
class Shot:
	"""A stretch of video between two of the biggest changes.

	With a generous budget this is a shot in the film-editing sense. With a tight one it is a
	segment: the span between the largest changes found, which may contain smaller cuts that
	were deliberately not located. The keyframe is one witness from the middle, not a summary.
	"""

	start: float
	end: float
	at: float  # the moment the keyframe was taken
	keyframe: bytes = field(repr=False)
	width: int = 0
	height: int = 0


@dataclass
class VideoSummary:
	"""What the video showed, at the cost of a few frames."""

	duration: float
	shots: list[Shot]
	samples_taken: int
	signature_mode: str  # 'in-page' or 'screenshot'
	video_width: int = 0
	video_height: int = 0
	ledger: TokenLedger = field(default_factory=TokenLedger)
	# Things the caller should know before trusting the result. A result that may be wrong and
	# does not say so is worse than an error.
	warnings: list[str] = field(default_factory=list)

	@property
	def cuts(self) -> list[float]:
		return [shot.start for shot in self.shots[1:]]

	def describe(self) -> str:
		"""The timeline as a line of text, which is the part a model can read for a few tokens."""
		spans = ' | '.join(f'{_clock(s.start)}-{_clock(s.end)}' for s in self.shots)
		text = (
			f'video {_clock(self.duration)}, {len(self.shots)} shots: {spans} '
			f'[{self.samples_taken} samples, {self.signature_mode} comparison]'
		)
		if self.warnings:
			text += ' WARNING: ' + ' '.join(self.warnings)
		self.ledger.add_text(text, 'timeline')
		return text

	def contact_sheet(self, columns: int = 4, tile_width: int = 320) -> bytes:
		"""Every keyframe on one JPEG, each labelled with its shot's time span."""
		from PIL import Image, ImageDraw, ImageFont

		assert self.shots, 'a summary with no shots has nothing to lay out'
		tiles = []
		for index, shot in enumerate(self.shots, start=1):
			image = Image.open(BytesIO(shot.keyframe)).convert('RGB')
			height = max(1, round(image.height * tile_width / image.width))
			tiles.append((index, shot, image.resize((tile_width, height), Image.Resampling.LANCZOS)))

		columns = max(1, min(columns, len(tiles)))
		rows = math.ceil(len(tiles) / columns)
		tile_height = max(tile.height for _, _, tile in tiles)
		gap = 4
		sheet = Image.new(
			'RGB', (columns * tile_width + (columns + 1) * gap, rows * tile_height + (rows + 1) * gap), (20, 20, 20)
		)
		draw = ImageDraw.Draw(sheet)
		font = ImageFont.load_default(size=max(11, tile_width // 20))
		for position, (index, shot, tile) in enumerate(tiles):
			x = gap + (position % columns) * (tile_width + gap)
			y = gap + (position // columns) * (tile_height + gap)
			sheet.paste(tile, (x, y))
			label = f'#{index}  {_clock(shot.start)}-{_clock(shot.end)}'
			box = draw.textbbox((x + 4, y + 4), label, font=font)
			draw.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill=(0, 0, 0))
			draw.text((x + 4, y + 4), label, fill=(255, 255, 255), font=font)

		out = BytesIO()
		sheet.save(out, format='JPEG', quality=80)
		self.ledger.add_image(sheet.width, sheet.height, 'contact sheet')
		return out.getvalue()


def _clock(seconds: float) -> str:
	minutes, rest = divmod(max(0.0, seconds), 60)
	return f'{int(minutes)}:{rest:04.1f}'


# Installed once per page. Everything that has to touch the video element lives here, so a
# sample is a single round trip. The element is held on the object, not re-queried, so a
# page that re-renders its player mid-watch cannot swap the subject under us.
_INSTALL_JS = """(() => {
	if (window.__buVideo) return true;
	const area = (el) => { const r = el.getBoundingClientRect(); return r.width * r.height; };
	// A box with area is not necessarily something a person can see.
	const visible = (el) => {
		const cs = getComputedStyle(el);
		return area(el) > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0;
	};
	const waitFor = (el, event, ms, what) => new Promise((resolve, reject) => {
		const timer = setTimeout(() => { el.removeEventListener(event, on); reject(new Error(what + ' timed out')); }, ms);
		const on = () => { clearTimeout(timer); el.removeEventListener(event, on); resolve(); };
		el.addEventListener(event, on);
	});
	const big = document.createElement('canvas'); big.width = 64; big.height = 36;
	const small = document.createElement('canvas'); small.width = 8; small.height = 8;
	const bigCtx = big.getContext('2d', { willReadFrequently: true });
	const smallCtx = small.getContext('2d', { willReadFrequently: true });
	bigCtx.imageSmoothingQuality = 'high'; smallCtx.imageSmoothingQuality = 'high';
	window.__buVideo = {
		el: null,
		async find(ms) {
			const videos = [...document.querySelectorAll('video')].filter(visible);
			videos.sort((a, b) => area(b) - area(a));
			const el = videos[0];
			if (!el) return null;
			this.el = el;
			el.pause();
			// With preload="none" the browser never volunteers the duration; asking for metadata
			// does not restart playback or touch the source, unlike load().
			if (el.readyState < 1 && el.preload === 'none') el.preload = 'metadata';
			if (el.readyState < 1) await waitFor(el, 'loadedmetadata', ms, 'loading video metadata');
			// Frames are only reachable from this document, so a larger player in an iframe may be
			// the one the page is really about. Reported, not chased.
			const framed = [...document.querySelectorAll('iframe')].filter((f) => area(f) > area(el)).length;
			return { duration: el.duration, width: el.videoWidth, height: el.videoHeight, framed };
		},
		async seek(t, ms) {
			const v = this.el;
			v.pause();  // a player that resumes itself would drift while we sample
			if (Math.abs(v.currentTime - t) > 1e-3) {
				const seeked = waitFor(v, 'seeked', ms, 'seek');
				v.currentTime = t;
				await seeked;
			}
			if (v.readyState < 2) await waitFor(v, 'canplay', ms, 'buffering');
			// Let the compositor paint the frame that was just decoded. requestAnimationFrame never
			// fires in a hidden tab, so the wait is bounded: the decoded frame is readable
			// regardless, and a wait with no timeout would make seek_timeout a promise we cannot keep.
			await Promise.race([
				new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))),
				new Promise((r) => setTimeout(r, 150)),
			]);
			return v.currentTime;
		},
		signature() {
			try {
				bigCtx.drawImage(this.el, 0, 0, 64, 36);
				smallCtx.drawImage(big, 0, 0, 8, 8);
				const px = smallCtx.getImageData(0, 0, 8, 8).data;
				const out = [];
				for (let i = 0; i < px.length; i += 4) out.push(px[i], px[i + 1], px[i + 2]);
				return { ok: true, sig: out };
			} catch (e) {
				return { ok: false, error: String(e) };
			}
		},
		rect() {
			this.el.scrollIntoView({ block: 'center', inline: 'center' });
			const r = this.el.getBoundingClientRect();
			return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
		},
	};
	return true;
})()"""


_HIDE_OVERLAYS_JS = (
	"document.querySelectorAll('[data-bu-overlay]').forEach((e) => e.style.setProperty('visibility', 'hidden', 'important'))"
)
_SHOW_OVERLAYS_JS = "document.querySelectorAll('[data-bu-overlay]').forEach((e) => e.style.removeProperty('visibility'))"


class VideoWatcher:
	"""Finds the shots in the page's video by seeking it, and keeps one keyframe of each."""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.browser_session = browser_session
		# Set by watch(); the CDP session every call in one watch goes through.
		self._cdp: Any = None
		self._mode = 'in-page'
		self._samples_taken = 0
		self._cache: dict[float, bytes] = {}
		self._seek_timeout = DEFAULT_SEEK_TIMEOUT

	async def watch(
		self,
		max_frames: int = 8,
		coarse: int | None = None,
		min_gap: float = 0.25,
		min_shot: float = 1.0,
		cut_threshold: int = CUT_THRESHOLD,
		seek_timeout: float = DEFAULT_SEEK_TIMEOUT,
	) -> VideoSummary:
		"""Locate the biggest cuts, up to `max_frames` shots, and keep one keyframe of each.

		`coarse` is how many intervals the first pass divides the video into. By default it is
		about one per second, from 8 to 60: a shot shorter than an interval can be missed, so
		raise it for fast-cut footage and lower it for a long talking head. A cut that would leave
		a shot shorter than `min_shot` seconds is not taken: a flash is a change, but not one worth
		a keyframe when the budget is a handful.
		"""
		assert max_frames >= 1, 'max_frames must be at least 1'
		assert min_gap > 0, 'min_gap must be positive'
		assert min_shot >= 0, 'min_shot cannot be negative'
		if not comparison_available():
			raise RuntimeError('Pillow is required to compare video frames, and it is not installed.')

		self._seek_timeout = seek_timeout
		self._samples_taken = 0
		self._cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		try:
			return await self._watch(max_frames, coarse, min_gap, min_shot, cut_threshold, screenshots=False)
		except _TaintedMidWatch:
			# Pixels became unreadable after some had been read. In-page and screenshot signatures
			# are different measurements (screenshots carry letterboxing and player controls), so
			# mixing them invents cuts; start over on one kind.
			logger.debug('🎞️ Canvas became tainted mid-watch; restarting on screenshot signatures')
			return await self._watch(max_frames, coarse, min_gap, min_shot, cut_threshold, screenshots=True)

	async def _watch(
		self, max_frames: int, coarse: int | None, min_gap: float, min_shot: float, cut_threshold: int, screenshots: bool
	) -> VideoSummary:
		self._cache = {}
		self._mode = 'screenshot' if screenshots else 'in-page'

		await self._eval(_INSTALL_JS)
		try:
			info = await self._eval(f'window.__buVideo.find({int(self._seek_timeout * 1000)})', await_promise=True)
		except RuntimeError as e:
			raise TimeoutError(f'The video never reported its length: {e}') from e
		if not info:
			raise NoVideoError('There is no visible <video> element on this page.')
		if not info['width'] or not info['height']:
			raise NoVideoError(
				'The media element has no picture (audio only, or nothing decoded), so there is nothing to look at.'
			)
		duration = info['duration']
		if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
			raise NoVideoError(f'The video has no finite duration ({duration!r}); a live stream cannot be seeked.')

		end = max(min_gap, duration - _END_MARGIN)
		intervals = coarse if coarse is not None else min(60, max(8, round(duration)))
		grid = [end * i / intervals for i in range(intervals + 1)]
		sigs = [await self._sample(t) for t in grid]

		warnings: list[str] = []
		if info['framed']:
			warnings.append(
				f'{info["framed"]} iframe(s) larger than this video exist, and the real player may be inside one; '
				'only the top document is searched.'
			)
		if not any(any(sig) for sig in sigs):
			warnings.append(
				'every sampled frame was pure black: either the video is black, or the player withholds its pixels (DRM).'
			)

		cuts = await self._locate_cuts(grid, cut_threshold, min_gap, min_shot, duration, wanted=max_frames - 1)

		bounds = [0.0, *cuts, duration]
		final = []
		for start, stop in zip(bounds, bounds[1:]):
			at = (start + stop) / 2
			jpeg, width, height = await self._keyframe(at)
			final.append(Shot(start, stop, at, jpeg, width, height))

		summary = VideoSummary(
			duration=duration,
			shots=final,
			samples_taken=self._samples_taken,
			signature_mode=self._mode,
			video_width=info['width'],
			video_height=info['height'],
			warnings=warnings,
		)
		assert all(shot.start <= shot.at <= shot.end for shot in summary.shots), 'a keyframe fell outside its shot'
		return summary

	async def _locate_cuts(
		self, grid: list[float], threshold: int, min_gap: float, min_shot: float, duration: float, wanted: int
	) -> list[float]:
		"""Pin down the `wanted` biggest changes, largest first, and stop.

		Every grid interval whose ends differ goes into a queue ordered by how much they
		differ. Taking the largest, halve it and look at the middle: the half that still differs
		goes back in the queue, and an interval narrower than `min_gap` is a located cut. A
		change is concentrated in one half of whatever contains it, so the largest change is
		followed all the way down before the next is started, which is what lets this stop
		after `wanted` cuts instead of locating every cut in a video only to discard most.
		"""
		queue: list[tuple[int, float, float]] = []
		for ta, tb in zip(grid, grid[1:]):
			if (d := signature_distance(self._cache[round(ta, 3)], self._cache[round(tb, 3)])) > threshold:
				heapq.heappush(queue, (-d, ta, tb))

		cuts: list[float] = []

		def take(cut: float) -> None:
			# Shots on both sides of the cut must last at least min_shot, or it is a sliver.
			if all(abs(cut - edge) >= min_shot for edge in (0.0, duration, *cuts)):
				cuts.append(cut)

		while queue and len(cuts) < wanted:
			_, ta, tb = heapq.heappop(queue)
			if tb - ta <= min_gap:
				take((ta + tb) / 2)
				continue
			mid = (ta + tb) / 2
			sa, sm, sb = self._cache[round(ta, 3)], await self._sample(mid), self._cache[round(tb, 3)]
			left, right = signature_distance(sa, sm), signature_distance(sm, sb)
			if left <= threshold and right <= threshold:
				# Each half looks like its own end but the ends differ: a gradual change, or a cut
				# sitting near the threshold under some motion. Follow the steeper half rather than
				# settling for the middle of what may be a very wide interval.
				heapq.heappush(queue, (-left, ta, mid) if left >= right else (-right, mid, tb))
				continue
			if left > threshold:
				heapq.heappush(queue, (-left, ta, mid))
			if right > threshold:
				heapq.heappush(queue, (-right, mid, tb))
		return sorted(cuts)

	async def _sample(self, t: float) -> bytes:
		"""A signature of the frame at `t`, cached because the same moment is asked for twice."""
		key = round(t, 3)
		if key in self._cache:
			return self._cache[key]
		await self._seek(t)
		self._samples_taken += 1
		signature = b''
		if self._mode == 'in-page':
			result = await self._eval('window.__buVideo.signature()')
			if result.get('ok'):
				signature = bytes(result['sig'])
			else:
				# Tainted canvas: the page may not read back a cross-origin video's pixels.
				logger.debug(f'🎞️ In-page comparison unavailable ({result.get("error")}); comparing screenshots')
				if self._cache:
					raise _TaintedMidWatch
				self._mode = 'screenshot'
		if self._mode == 'screenshot':
			jpeg, _, _ = await self._screenshot(scale_to=160)
			signature = frame_signature(jpeg)
		assert signature, 'a sample must produce a signature'
		self._cache[key] = signature
		return signature

	async def _seek(self, t: float) -> None:
		try:
			await self._eval(f'window.__buVideo.seek({t!r}, {int(self._seek_timeout * 1000)})', await_promise=True)
		except RuntimeError as e:
			raise TimeoutError(f'Could not seek the video to {t:.2f}s: {e}') from e

	async def _keyframe(self, t: float) -> tuple[bytes, int, int]:
		await self._seek(t)
		return await self._screenshot(scale_to=KEYFRAME_MAX_WIDTH)

	async def _screenshot(self, scale_to: int) -> tuple[bytes, int, int]:
		"""JPEG of the video element alone, no wider than `scale_to` CSS pixels.

		Anything marked `data-bu-overlay` (the token meter, the cursor) is hidden for the
		capture: it is ours, not the video's, and a keyframe that carries it would show the
		model our instrumentation as if it were content.
		"""
		rect = await self._eval('window.__buVideo.rect()')
		scale = min(1.0, scale_to / rect['width'])
		await self._eval(_HIDE_OVERLAYS_JS)
		try:
			shot = await self._cdp.cdp_client.send.Page.captureScreenshot(
				params={
					'format': 'jpeg',
					'quality': 70,
					'clip': {'x': rect['x'], 'y': rect['y'], 'width': rect['width'], 'height': rect['height'], 'scale': scale},
				},
				session_id=self._cdp.session_id,
			)
		finally:
			await self._eval(_SHOW_OVERLAYS_JS)
		jpeg = base64.b64decode(shot['data'])
		return jpeg, round(rect['width'] * scale), round(rect['height'] * scale)

	async def _eval(self, expression: str, await_promise: bool = False):
		result = await self._cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': expression, 'returnByValue': True, 'awaitPromise': await_promise},
			session_id=self._cdp.session_id,
		)
		if details := result.get('exceptionDetails'):
			description = (details.get('exception') or {}).get('description') or details.get('text') or json.dumps(details)[:200]
			raise RuntimeError(description.splitlines()[0])
		return result['result'].get('value')


__all__ = ['CUT_THRESHOLD', 'NoVideoError', 'Shot', 'TokenLedger', 'VideoSummary', 'VideoWatcher', 'estimate_image_tokens']
