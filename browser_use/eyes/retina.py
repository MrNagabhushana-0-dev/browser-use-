"""The Python half of the retina: attach it to a tab, and keep what it pushes.

`retina.js` measures inside the page; this receives the measurements. Delivery is a push,
not a poll: the page calls a CDP binding and Chrome forwards each call as a
`Runtime.bindingCalled` event, so a frame sampled in the page is in Python a fraction of a
second later without anyone asking for it.

The script runs in an *isolated world*: it shares the page's DOM, video elements and audio,
but not its JavaScript globals. The page cannot see `__retina`, cannot overwrite the
functions it calls, and does not notice a binding on its own `window`. The same world is
re-created on every navigation by `Page.addScriptToEvaluateOnNewDocument`, so the retina
survives a reload or a move to the next page without being re-attached.

What is kept is small: a 16x16 luma grid per sampled frame (256 bytes), one short row of
audio features per ~22 ms, and the page's own events. A minute of watching is well under a
megabyte. Keyframe *images* stay in the page until asked for.
"""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from browser_use.browser.capture import capture_screenshot

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

WORLD_NAME = 'browser_use_retina'
BINDING = '__retina_emit'
_BINDING_EVENT = 'Runtime.bindingCalled'
_SCRIPT = (Path(__file__).parent / 'retina.js').read_text()

# How much history is kept, in samples. At 10 fps and ~45 audio hops a second, this is a
# couple of minutes: far more than one watch needs, and bounded.
MAX_FRAMES = 1500
MAX_HOPS = 6000
# A cross-origin video without CORS cannot be read by the page ("tainted"); its pictures are taken from the
# compositor instead, at this rate, with sample numbers from here on so they never collide with the page's own.
COMPOSITOR_FPS = 6.0
COMPOSITOR_SEQ = 1_000_000_000
MAX_LOCAL_KEYFRAMES = 400
MAX_EVENTS = 2000


def media_time_at(readings: 'Sequence[tuple[float, float]]', wall: float) -> float | None:
	"""A film's time at wall-clock `wall` (seconds since the epoch), from readings of its clock as (wall, media time)
	pairs in the order taken: interpolated between the two around it, so a clock that stood still (a stall) reads flat
	and one that ran reads its slope. Before the first reading the film is taken to have played at 1x; after the last
	it is not run on. Between readings that jump (a seek), whichever side is nearer."""
	readings = list(readings)  # a deque cannot be sliced
	if not readings:
		return None
	first_w, first_t = readings[0]
	if wall <= first_w:
		return max(0.0, first_t - (first_w - wall))
	for (w0, t0), (w1, t1) in zip(readings, readings[1:]):
		if w0 <= wall <= w1:
			span = w1 - w0
			if span <= 0 or t1 < t0 or t1 - t0 > span + 1.0:
				return t0 if wall - w0 < w1 - wall else t1
			return t0 + (t1 - t0) * (wall - w0) / span
	return readings[-1][1]


@dataclass(slots=True)
class FrameSample:
	"""One sampled video frame, as the retina saw it."""

	seq: int
	vid: int
	t: float  # media time, seconds
	wall: float  # time.monotonic() when it arrived
	luma: bytes = field(repr=False)  # 16x16, row-major
	rgb: tuple[int, int, int] = (0, 0, 0)
	has_keyframe: bool = False
	colours: bytes = field(default=b'', repr=False)  # 4x4 RGB grid, 48 bytes


@dataclass(slots=True)
class AudioHop:
	"""~22 ms of the attended video's sound, reduced to features."""

	vid: int
	t: float  # media time at arrival, seconds (accurate to a few tens of ms)
	wall: float
	rms_db: float
	zcr: float
	centroid_hz: float
	flux: float
	flatness: float
	peak_hz: float
	bands: bytes = field(repr=False)  # 24 log-spaced bands, 50 Hz-11 kHz; byte = (dB + 40) * 2.55
	pcm: bytes = field(default=b'', repr=False)  # int16 little-endian mono at 16 kHz, only when the retina runs with pcm=True


@dataclass(slots=True)
class RetinaEvent:
	type: str
	wall: float
	data: dict[str, Any] = field(default_factory=dict)


class Retina:
	"""Attach the in-page retina to one tab and buffer what it sees and hears."""

	def __init__(
		self,
		browser_session: 'BrowserSession',
		*,
		fps: float = 10.0,
		audio: bool = True,
		pcm: bool = False,
		listen: bool = False,
		keyframe_width: int = 320,
	) -> None:
		assert fps > 0, 'fps must be positive'
		self.browser_session = browser_session
		self.options = {'fps': fps, 'audio': audio, 'pcm': pcm, 'listen': listen, 'thumbWidth': keyframe_width}
		self.frames: deque[FrameSample] = deque(maxlen=MAX_FRAMES)
		self.hops: deque[AudioHop] = deque(maxlen=MAX_HOPS)
		self.events: deque[RetinaEvent] = deque(maxlen=MAX_EVENTS)
		self.state: dict[str, Any] = {}
		self.keyframes_timeout = 20.0  # seconds to wait for the page to hand over keyframe images
		self.attended: dict[str, Any] = {}
		self.page_since = 0.0  # monotonic time the current page's first report arrived
		self.target_id: str | None = None
		self._session_id: str | None = None
		self._cdp: Any = None
		self._script_id: str | None = None
		self._incumbent: Callable[..., Any] | None = None
		self._handler: Callable[..., Any] | None = None
		self._listeners: list[Callable[[list[FrameSample], list[AudioHop], list[RetinaEvent]], None]] = []
		self._arrived = asyncio.Event()
		self.batches = 0
		self._compositor: asyncio.Task | None = None
		self._compositor_vid = 0
		self._compositor_seq = COMPOSITOR_SEQ
		self._local_keyframes: dict[int, bytes] = {}
		self._state_wall = 0.0
		self._compositor_context: int | None = None

	@property
	def running(self) -> bool:
		return self._handler is not None

	# -- lifecycle -----------------------------------------------------------------------

	async def start(self, target_id: str | None = None) -> dict[str, Any]:
		"""Attach to the focused tab (or `target_id`) and start measuring. Idempotent."""
		if self.running:
			return self.state
		cdp = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
		self._cdp = cdp
		self._session_id = cdp.session_id
		self.target_id = cdp.target_id
		send = cdp.cdp_client.send

		# One handler slot per CDP method in cdp-use; chain onto whoever holds it.
		registry = getattr(self.browser_session.cdp_client, '_event_registry', None)
		handlers = getattr(registry, '_handlers', None)
		self._incumbent = handlers.get(_BINDING_EVENT) if isinstance(handlers, dict) else None
		self._handler = self._on_binding
		self.browser_session.cdp_client.register.Runtime.bindingCalled(self._handler)

		await send.Runtime.enable(session_id=self._session_id)
		await send.Runtime.addBinding(params={'name': BINDING, 'executionContextName': WORLD_NAME}, session_id=self._session_id)
		boot = f'window.__retinaOpts = {json.dumps(self.options)}; window.__retinaAutostart = true;\n'
		added = await send.Page.addScriptToEvaluateOnNewDocument(
			params={'source': boot + _SCRIPT, 'worldName': WORLD_NAME, 'runImmediately': False},
			session_id=self._session_id,
		)
		self._script_id = added.get('identifier')
		# The document that is already loaded does not get the new-document script.
		await self.evaluate(f'window.__retinaOpts = {json.dumps(self.options)};\n' + _SCRIPT)
		state = await self.evaluate(f'window.__retina.start({json.dumps(self.options)})')
		self.state = state or {}
		logger.debug(f'👁️ Retina attached to {self.target_id and self.target_id[-4:]}: {self.state}')
		return self.state

	async def stop(self) -> None:
		self._stop_compositor()
		if not self.running:
			return
		try:
			await self.evaluate('window.__retina && window.__retina.stop()')
		except Exception:
			pass
		if self._script_id and self._cdp is not None:
			try:
				await self._cdp.cdp_client.send.Page.removeScriptToEvaluateOnNewDocument(
					params={'identifier': self._script_id}, session_id=self._session_id
				)
			except Exception:
				pass
		registry = getattr(self.browser_session.cdp_client, '_event_registry', None)
		handlers = getattr(registry, '_handlers', None)
		if isinstance(handlers, dict) and handlers.get(_BINDING_EVENT) is self._handler:
			if self._incumbent is not None:
				self.browser_session.cdp_client.register.Runtime.bindingCalled(self._incumbent)
			else:
				handlers.pop(_BINDING_EVENT, None)
		self._handler = None
		self._script_id = None

	# -- talking to the page -------------------------------------------------------------

	async def _context_id(self) -> int:
		"""The isolated world's context in the tab's main frame (created if it does not exist)."""
		assert self._cdp is not None, 'retina not started'
		send = self._cdp.cdp_client.send
		tree = await send.Page.getFrameTree(session_id=self._session_id)
		frame_id = tree['frameTree']['frame']['id']
		world = await send.Page.createIsolatedWorld(
			params={'frameId': frame_id, 'worldName': WORLD_NAME}, session_id=self._session_id
		)
		return world['executionContextId']

	async def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
		"""Run JS in the retina's world and return its (JSON) value."""
		assert self._cdp is not None, 'retina not started'
		cdp = self._cdp

		async def run() -> dict[str, Any]:
			# Finding the world needs the renderer too: it shares the timeout rather than wait unbounded.
			context_id = await self._context_id()
			return await cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': expression, 'contextId': context_id, 'awaitPromise': True, 'returnByValue': True},
				session_id=self._session_id,
			)

		result = await asyncio.wait_for(run(), timeout=timeout)
		if result.get('exceptionDetails'):
			details = result['exceptionDetails']
			text = (details.get('exception') or {}).get('description') or details.get('text')
			raise RuntimeError(f'retina script error: {text}')
		return (result.get('result') or {}).get('value')

	async def keyframes(self, seqs: list[int]) -> list[bytes | None]:
		"""JPEG bytes of the keyframes taken at these sample numbers (None if evicted, or if the page did not answer)."""
		return (await self.read_keyframes(seqs))[0]

	async def read_keyframes(self, seqs: list[int], timeout: float | None = None) -> tuple[list[bytes | None], str | None]:
		"""Like keyframes(), plus why they are all missing when the page did not hand them over in time (by `timeout`,
		default `keyframes_timeout`)."""
		if not seqs:
			return [], None
		page_seqs = [s for s in seqs if s < COMPOSITOR_SEQ]
		got: dict[int, bytes | None] = {s: self._local_keyframes.get(s) for s in seqs if s >= COMPOSITOR_SEQ}
		why = None
		if page_seqs:
			try:
				urls = await self.evaluate(
					f'window.__retina.keyframes({json.dumps(page_seqs)})',
					timeout=self.keyframes_timeout if timeout is None else timeout,
				)
				got.update(zip(page_seqs, [_decode_data_url(u) for u in (urls or [None] * len(page_seqs))]))
			except TimeoutError:
				# What was seen and heard is already held; losing the pictures must not lose the watch.
				why = await self._why_no_answer()
				logger.warning(f'👁️ keyframes: {why}')
		return [got.get(s) for s in seqs], why

	# -- a cross-origin video, seen from the compositor -----------------------------------------------------

	def _watch_compositor(self, vid: int) -> None:
		if not vid or (self._compositor and not self._compositor.done() and self._compositor_vid == vid):
			return
		self._stop_compositor()
		self._compositor_vid = vid
		self._compositor = asyncio.create_task(self._compositor_loop(vid), name='retina_compositor')

	def _stop_compositor(self) -> None:
		if self._compositor is not None and not self._compositor.done():
			self._compositor.cancel()
		self._compositor = None
		self._compositor_vid = 0

	async def _compositor_loop(self, vid: int) -> None:
		"""While a tainted video is attended, sample its rectangle as the compositor draws it: cut from the frames of a
		screencast of its own, or, if none can be opened, captured."""
		try:
			await self._compositor_by_screencast(vid)
		except asyncio.CancelledError:
			raise
		except Exception as e:
			logger.debug(f'retina: no screencast for compositor sight, capturing instead: {type(e).__name__}: {e}')
		await self._compositor_by_capture(vid)

	def _emit_compositor(self, sample: FrameSample | None) -> None:
		if sample is None:
			return
		self.frames.append(sample)
		for listener in self._listeners:
			try:
				listener([sample], [], [])
			except Exception as e:
				logger.debug(f'retina listener failed: {type(e).__name__}: {e}')
		self._arrived.set()

	async def _compositor_by_capture(self, vid: int) -> None:
		period = 1.0 / COMPOSITOR_FPS
		while self.running and self.attended.get('vid') == vid:
			started = time.monotonic()
			try:
				self._emit_compositor(await self._compositor_frame(vid))
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.debug(f'retina: compositor frame failed: {type(e).__name__}: {e}')
			await asyncio.sleep(max(0.0, period - (time.monotonic() - started)))

	async def _compositor_by_screencast(self, vid: int) -> None:
		"""A screencast of the tab on a DevTools connection of its own (an event has one handler per connection, and the
		recorder holds the session's), its newest frame cut to the video's box COMPOSITOR_FPS times a second. Each
		capture of the whole viewport took ~95 ms on a busy page; this takes none. Returns when the video is no longer
		attended; raises if the screencast cannot be had."""
		import json as _json
		from urllib.parse import urlparse

		import websockets

		assert self._cdp is not None
		where = urlparse(self.browser_session.cdp_url or '')
		if not (where.hostname and where.port):
			raise RuntimeError(f'no local DevTools address in {self.browser_session.cdp_url!r}')
		url = f'ws://{where.hostname}:{where.port}/devtools/page/{self._cdp.target_id}'
		period = 1.0 / COMPOSITOR_FPS
		async with await websockets.connect(url, max_size=None, proxy=None, open_timeout=5) as ws:
			newest: list[tuple[bytes, dict] | None] = [None]

			async def read() -> None:
				async for raw in ws:
					msg = _json.loads(raw)
					if msg.get('method') != 'Page.screencastFrame':
						continue
					params = msg['params']
					await ws.send(
						_json.dumps({'id': 2, 'method': 'Page.screencastFrameAck', 'params': {'sessionId': params['sessionId']}})
					)
					newest[0] = (base64.b64decode(params['data']), params.get('metadata') or {})

			start = {'format': 'jpeg', 'quality': 80, 'maxWidth': 1280, 'maxHeight': 1280, 'everyNthFrame': 1}
			await ws.send(_json.dumps({'id': 1, 'method': 'Page.startScreencast', 'params': start}))
			reader = asyncio.create_task(read())
			clock: deque[tuple[float, float]] = deque(maxlen=64)  # (wall, media time), read each tick
			try:
				while self.running and self.attended.get('vid') == vid:
					started = time.monotonic()
					if reader.done():
						reader.result()
						raise RuntimeError('the screencast connection closed')
					frame, newest[0] = newest[0], None
					before = time.time()
					media_t = await self._media_time(vid)
					if media_t is not None:
						clock.append(((before + time.time()) / 2, media_t))
					if frame is not None:
						try:
							self._emit_compositor(await self._compositor_from(vid, *frame, clock))
						except asyncio.CancelledError:
							raise
						except Exception as e:
							logger.debug(f'retina: compositor frame failed: {type(e).__name__}: {e}')
					await asyncio.sleep(max(0.0, period - (time.monotonic() - started)))
			finally:
				reader.cancel()
				await asyncio.gather(reader, return_exceptions=True)

	async def _compositor_from(
		self, vid: int, data: bytes, meta: dict, clock: 'Sequence[tuple[float, float]]' = ()
	) -> FrameSample | None:
		"""A sample of the attended video's box, cut from one screencast frame, labelled with the film's time when Chrome
		drew the frame (its clock as read around then: `clock`)."""
		import io

		from PIL import Image

		state = self.state if self.state.get('vid') == vid else {}
		if state.get('paused'):
			return None
		rect = self.attended.get('rect') or []
		if len(rect) != 4 or rect[2] < 8 or rect[3] < 8:
			return None
		with Image.open(io.BytesIO(data)) as img:
			css_w = float(meta.get('deviceWidth') or img.width)
			css_h = float(meta.get('deviceHeight') or img.height)
			px = img.width / max(1.0, css_w)
			left, top = max(0.0, float(rect[0])), max(0.0, float(rect[1]))
			right, bottom = min(float(rect[0] + rect[2]), css_w), min(float(rect[1] + rect[3]), css_h)
			if right - left < 8 or bottom - top < 8:
				return None  # scrolled out of view
			off = float(meta.get('offsetTop') or 0.0)
			box = (round(left * px), round((top + off) * px), round(right * px), round((bottom + off) * px))
			part = img.convert('RGB').crop(box)
		thumb_width = int(self.options.get('thumbWidth') or 320)
		if part.width > thumb_width:
			part = part.resize((thumb_width, max(1, round(part.height * thumb_width / part.width))), Image.Resampling.BOX)
		luma = part.convert('L').resize((16, 16), Image.Resampling.BOX).tobytes()
		colours = part.resize((4, 4), Image.Resampling.BOX).tobytes()
		mean = part.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
		buf = io.BytesIO()
		part.save(buf, 'JPEG', quality=70)
		# Media time: the video's clock at the moment Chrome drew the frame (it stamps it), from readings around then. The
		# time read now less the frame's age went back in a stall: the clock had stood still for part of that age.
		drawn = float(meta.get('timestamp') or 0.0)
		at = media_time_at(clock, drawn) if drawn else (clock[-1][1] if clock else None)
		if at is not None:
			t = at
		else:
			t = float(state.get('t') or 0.0) + max(0.0, time.monotonic() - self._state_wall) if state else 0.0
		self._compositor_seq += 1
		seq = self._compositor_seq
		self._local_keyframes[seq] = buf.getvalue()
		while len(self._local_keyframes) > MAX_LOCAL_KEYFRAMES:
			self._local_keyframes.pop(next(iter(self._local_keyframes)))
		return FrameSample(seq, vid, t, time.monotonic(), luma, tuple(mean)[:3], True, colours)  # type: ignore[arg-type]

	async def _compositor_frame(self, vid: int) -> FrameSample | None:
		import io

		from PIL import Image

		state = self.state if self.state.get('vid') == vid else {}
		if state.get('paused') or self._cdp is None:
			return None  # the page samples only while it plays; so does this
		rect = self.attended.get('rect') or []
		if len(rect) != 4 or rect[2] < 8 or rect[3] < 8:
			return None
		send = self._cdp.cdp_client.send
		metrics = await send.Page.getLayoutMetrics(session_id=self._session_id)
		vp = metrics['cssLayoutViewport']
		left, top = max(0.0, float(rect[0])), max(0.0, float(rect[1]))
		width = min(float(rect[0] + rect[2]), float(vp['clientWidth'])) - left
		height = min(float(rect[1] + rect[3]), float(vp['clientHeight'])) - top
		if width < 8 or height < 8:
			return None  # scrolled out of view
		thumb_width = float(self.options.get('thumbWidth') or 320)
		clip = {
			'x': vp['pageX'] + left,
			'y': vp['pageY'] + top,
			'width': width,
			'height': height,
			'scale': min(1.0, thumb_width / width),
		}
		before = await self._media_time(vid)
		shot = await capture_screenshot(self._cdp.cdp_client, {'format': 'jpeg', 'quality': 70, 'clip': clip}, self._session_id)
		after = await self._media_time(vid)
		jpeg = base64.b64decode(shot['data'])
		with Image.open(io.BytesIO(jpeg)) as img:
			rgb_img = img.convert('RGB')
			luma = rgb_img.convert('L').resize((16, 16), Image.Resampling.BOX).tobytes()
			colours = rgb_img.resize((4, 4), Image.Resampling.BOX).tobytes()
			mean = rgb_img.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
		# Media time: read from the video on both sides of the capture. Failing that, the page's last report of it run
		# forward to now, which runs ahead while a stream stalls (found on Wikimedia Commons: labels ran a second
		# ahead, then went back).
		reads = [r for r in (before, after) if r is not None]
		if reads:
			t = sum(reads) / len(reads)
		else:
			t = float(state.get('t') or 0.0) + max(0.0, time.monotonic() - self._state_wall) if state else 0.0
		self._compositor_seq += 1
		seq = self._compositor_seq
		self._local_keyframes[seq] = jpeg
		while len(self._local_keyframes) > MAX_LOCAL_KEYFRAMES:
			self._local_keyframes.pop(next(iter(self._local_keyframes)))
		return FrameSample(seq, vid, t, time.monotonic(), luma, tuple(mean)[:3], True, colours)  # type: ignore[arg-type]

	async def _media_time(self, vid: int) -> float | None:
		"""The attended video's currentTime, read in the retina's world now, or None if it is another item or the read
		failed. The world's context is kept between reads and found again when it goes (a navigation)."""
		assert self._cdp is not None
		try:
			if self._compositor_context is None:
				self._compositor_context = await self._context_id()
			r = await self._cdp.cdp_client.send.Runtime.evaluate(
				params={
					'expression': 'window.__retina ? window.__retina.mediaTime() : null',
					'contextId': self._compositor_context,
					'returnByValue': True,
				},
				session_id=self._session_id,
			)
		except Exception:
			self._compositor_context = None
			return None
		if r.get('exceptionDetails'):
			self._compositor_context = None
			return None
		value = (r.get('result') or {}).get('value')
		return float(value[1]) if isinstance(value, list) and len(value) == 2 and value[0] == vid else None

	async def _why_no_answer(self, probe_s: float = 2.0) -> str:
		"""After a read timed out: is the renderer answering at all, or only that read stuck?"""
		assert self._cdp is not None
		try:
			await asyncio.wait_for(
				self._cdp.cdp_client.send.Runtime.evaluate(params={'expression': '1'}, session_id=self._session_id),
				timeout=probe_s,
			)
		except TimeoutError:
			return f'the page did not answer in {self.keyframes_timeout:.0f}s, and its main thread is busy or hung'
		except Exception as e:
			return f'the page did not answer in {self.keyframes_timeout:.0f}s, and then failed: {type(e).__name__}: {e}'
		return f'the page did not answer in {self.keyframes_timeout:.0f}s, though it answers now (the read itself stuck)'

	async def snapshot(self, width: int = 480) -> bytes | None:
		"""A JPEG of the attended video's current frame, drawn from the element (not the screen)."""
		return _decode_data_url(await self.evaluate(f'window.__retina.snapshot({int(width)})'))

	async def set_listen(self, on: bool) -> dict[str, Any]:
		self.options['listen'] = bool(on)
		return await self.evaluate(f'window.__retina.setListen({json.dumps(bool(on))})')

	async def page_state(self) -> dict[str, Any]:
		return await self.evaluate('window.__retina ? window.__retina.state() : null') or {}

	# -- receiving -----------------------------------------------------------------------

	def on_batch(self, listener: Callable[[list[FrameSample], list[AudioHop], list[RetinaEvent]], None]) -> None:
		"""Call `listener(frames, hops, events)` for every batch as it arrives."""
		self._listeners.append(listener)

	async def wait_for_data(self, timeout: float) -> bool:
		"""Wait until the next batch arrives. False on timeout."""
		self._arrived.clear()
		try:
			await asyncio.wait_for(self._arrived.wait(), timeout)
			return True
		except TimeoutError:
			return False

	def _on_binding(self, event: dict[str, Any], session_id: str | None = None) -> Any:
		# Every tab's retina calls the same binding name; only this tab's calls are ours.
		if event.get('name') != BINDING or (session_id is not None and session_id != self._session_id):
			if self._incumbent is not None:
				return self._incumbent(event, session_id)
			return None
		try:
			batch = json.loads(event.get('payload') or '{}')
			self._ingest(batch)
		except Exception as e:
			logger.debug(f'retina: dropped a malformed batch: {type(e).__name__}: {e}')
		return None

	def _ingest(self, batch: dict[str, Any]) -> None:
		now = time.monotonic()
		frames: list[FrameSample] = []
		for seq, vid, t, _page_ms, luma, rgb, thumb, c4 in batch.get('f') or []:
			frames.append(
				FrameSample(seq, vid, float(t), now, base64.b64decode(luma), tuple(rgb), bool(thumb), base64.b64decode(c4))
			)
		hops: list[AudioHop] = []
		for row in batch.get('a') or []:
			t, rms, zcr, cen, flux, flat, peak, bands, pcm, vid = row
			hops.append(
				AudioHop(
					vid,
					float(t),
					now,
					rms,
					zcr,
					cen,
					flux,
					flat,
					peak,
					base64.b64decode(bands),
					base64.b64decode(pcm) if pcm else b'',
				)
			)
		events: list[RetinaEvent] = []
		for e in batch.get('e') or []:
			kind = e.pop('type', 'unknown')
			events.append(RetinaEvent(kind, now, e))
			if kind == 'state':
				if e.get('url') != self.state.get('url'):
					self.page_since = now
				self.state = e
				self._state_wall = now
				if not e.get('vid') and self.attended.get('vid'):
					self.attended = {'vid': 0}  # the page attends to nothing (a new page, or the video left it)
			elif kind == 'attend':
				self.attended = e
				if e.get('vid') != self._compositor_vid:
					self._stop_compositor()
			elif kind == 'tainted':
				self._watch_compositor(int(e.get('vid') or 0))
		self.frames.extend(frames)
		self.hops.extend(hops)
		self.events.extend(events)
		self.batches += 1
		for listener in self._listeners:
			try:
				listener(frames, hops, events)
			except Exception as e:
				logger.debug(f'retina listener failed: {type(e).__name__}: {e}')
		self._arrived.set()


def _decode_data_url(url: Any) -> bytes | None:
	if not isinstance(url, str) or ',' not in url:
		return None
	return base64.b64decode(url.split(',', 1)[1])
