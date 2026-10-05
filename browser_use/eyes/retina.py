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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
MAX_EVENTS = 2000


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

	async def read_keyframes(self, seqs: list[int]) -> tuple[list[bytes | None], str | None]:
		"""Like keyframes(), plus why they are all missing when the page did not hand them over in time."""
		if not seqs:
			return [], None
		try:
			urls = await self.evaluate(f'window.__retina.keyframes({json.dumps(list(seqs))})', timeout=self.keyframes_timeout)
		except TimeoutError:
			# What was seen and heard is already held; losing the pictures must not lose the watch.
			why = await self._why_no_answer()
			logger.warning(f'👁️ keyframes: {why}')
			return [None] * len(seqs), why
		return [_decode_data_url(u) for u in (urls or [None] * len(seqs))], None

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
				if not e.get('vid') and self.attended.get('vid'):
					self.attended = {'vid': 0}  # the page attends to nothing (a new page, or the video left it)
			elif kind == 'attend':
				self.attended = e
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
