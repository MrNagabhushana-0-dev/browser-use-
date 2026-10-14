"""Reflexes: the agent's rules, run by the page every animation frame, with real input out within milliseconds.

A model thinks in seconds; a game, a live feed or a form that reacts must be answered within a frame or two. So
the model does not sit in that loop. After the dual-system designs of 2025-2026 (a slow reasoner steering a fast
policy that acts at frame rate: Figure's Helix at 7-9 Hz over 200 Hz; the slow-fast channel of real-time game agents)
and event-gated streaming perception (StreamMind: the model is invoked only when an event it cares about happens),
the agent writes rules once:

	{'when': "see.locate([255,0,0]).x < see.locate([0,0,255]).x", 'hold': 'ArrowLeft'}
	{'when': "see.text('#status') === 'GAME OVER'", 'notify': 'game over'}

The page evaluates every rule each frame against what it draws (reflex.js), and on a rule's edges Python sends real
key or mouse input over CDP at once; `wait()` returns when a `notify` rule fires. Between those, the agent spends
nothing: no screenshots, no polling. stats() reports the decision-to-delivery latency, measured on the page's clock.
"""

import asyncio
import base64
import io
import json
import logging
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from browser_use.browser.capture import capture_screenshot

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

WORLD_NAME = 'browser_use_reflex'
BINDING = '__browserUseReflex'
_BINDING_EVENT = 'Runtime.bindingCalled'
_SCRIPT = (Path(__file__).parent / 'reflex.js').read_text()
# The screen picture fed to the rules for what the page cannot read itself (a cross-origin video or frame): this wide,
# as PNG so colours are exact
SCREEN_WIDTH = 240
# A screencast this long without a frame is topped up with one capture, in case it dropped the frame an animation ended on
SCREEN_QUIET_S = 0.3
# Captures failing for this long mean the tab or its browser is gone: the feed stops
SCREEN_GIVE_UP_S = 5.0

# windowsVirtualKeyCode for the keys games and players use most; letters and digits are their ASCII codes
_KEY_CODES = {
	'ArrowLeft': 37,
	'ArrowUp': 38,
	'ArrowRight': 39,
	'ArrowDown': 40,
	'Enter': 13,
	' ': 32,
	'Space': 32,
	'Escape': 27,
	'Tab': 9,
	'Backspace': 8,
	'Shift': 16,
	'Control': 17,
}


class ReflexRule(BaseModel):
	"""When `when` (a JavaScript expression over `see`) holds: hold a key while it does, or tap a key, click, or
	notify the agent as it becomes true."""

	model_config = ConfigDict(extra='forbid')

	when: str = Field(min_length=1, max_length=2000)
	hold: str | None = None
	key: str | None = None
	click: str | None = Field(default=None, description='expression giving [x, y] in viewport CSS px')
	notify: str | None = Field(default=None, max_length=200)
	cooldown_ms: int = Field(default=0, ge=0, le=60000)

	@model_validator(mode='after')
	def _does_something(self) -> 'ReflexRule':
		assert self.hold or self.key or self.click or self.notify, 'a rule needs hold, key, click or notify'
		assert not (self.hold and (self.key or self.click)), 'a held key is its own action'
		return self


class ReflexEvent(BaseModel):
	model_config = ConfigDict(extra='forbid')

	label: str
	rule: int
	page_ms: float  # the page's clock when the rule fired
	media_t: float | None = None  # the playing video's own time then, when there is one
	wall: float  # time.monotonic() when it reached Python


class _NoScreencast(Exception):
	"""No DevTools connection of its own could be opened for the screencast."""


def _rgb(png: bytes) -> tuple[int, int, bytes]:
	"""(width, height, raw RGB) of a picture."""
	from PIL import Image

	with Image.open(io.BytesIO(png)) as img:
		rgb = img.convert('RGB')
		return rgb.width, rgb.height, rgb.tobytes()


def _key_params(key: str, kind: str) -> dict[str, Any]:
	code = _KEY_CODES.get(key) or (ord(key.upper()) if len(key) == 1 else 0)
	name = 'Space' if key in (' ', 'Space') else (f'Key{key.upper()}' if len(key) == 1 and key.isalpha() else key)
	params: dict[str, Any] = {'type': kind, 'key': ' ' if key == 'Space' else key, 'code': name, 'windowsVirtualKeyCode': code}
	if kind == 'keyDown' and len(params['key']) == 1:
		params['text'] = params['key']
	return params


class Reflexes:
	"""Rules on one tab: install(), wait() for what they notify, stats(), clear()."""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.session = browser_session
		self.rules: list[ReflexRule] = []
		self.events: asyncio.Queue[ReflexEvent] = asyncio.Queue()
		self.errors: list[str] = []
		self._latencies: list[float] = []
		self._actions = 0
		self._held: set[str] = set()
		self._cdp: Any = None
		self._session_id: str | None = None
		self._handler: Any = None
		self._incumbent: Any = None
		self._tasks: set[asyncio.Task] = set()
		self._context_id: int | None = None
		self._screen_task: asyncio.Task | None = None
		self._screen_ms: list[float] = []  # how long each screen picture took, capture to fed
		self._screen_fed = 0
		self._screen_seq = 0
		self._screen_source: str | None = None  # 'screencast' or 'captures', once the feed has started

	@property
	def installed(self) -> bool:
		return self._handler is not None

	async def install(self, rules: list[dict[str, Any] | ReflexRule]) -> int:
		"""Run these rules on the focused tab from the next frame on, replacing any installed before."""
		checked = [r if isinstance(r, ReflexRule) else ReflexRule.model_validate(r) for r in rules]
		assert 1 <= len(checked) <= 50, 'between 1 and 50 rules'
		await self.clear()
		# New rules start clean: an event the old ones raised that nobody waited for is not handed to the next wait,
		# and the stats are the new rules' own (found live: a wait returned a replaced rule's "30 s up")
		self.events = asyncio.Queue()
		self._latencies.clear()
		self._actions = 0
		self.errors.clear()
		self._screen_ms.clear()
		self._screen_fed = 0
		self._screen_source = None
		cdp = await self.session.get_or_create_cdp_session(focus=False)
		self._cdp, self._session_id = cdp, cdp.session_id
		send = cdp.cdp_client.send
		registry = getattr(self.session.cdp_client, '_event_registry', None)
		handlers = getattr(registry, '_handlers', None)
		self._incumbent = handlers.get(_BINDING_EVENT) if isinstance(handlers, dict) else None
		self._handler = self._on_binding
		self.session.cdp_client.register.Runtime.bindingCalled(self._handler)
		tree = await send.Page.getFrameTree(session_id=self._session_id)
		world = await send.Page.createIsolatedWorld(
			params={'frameId': tree['frameTree']['frame']['id'], 'worldName': WORLD_NAME}, session_id=self._session_id
		)
		# After the world exists: a binding by context name reaches the worlds that exist when it is added (the page's
		# own main world never gets it, so the page cannot forge the reflexes' input)
		await send.Runtime.addBinding(params={'name': BINDING, 'executionContextName': WORLD_NAME}, session_id=self._session_id)
		source = _SCRIPT.replace('__RULES__', json.dumps([r.model_dump() for r in checked])).replace('__BINDING__', BINDING)
		result = await send.Runtime.evaluate(
			params={'expression': source, 'contextId': world['executionContextId'], 'returnByValue': True},
			session_id=self._session_id,
		)
		if result.get('exceptionDetails'):
			await self.clear()
			raise ValueError(f'the rules did not compile: {result["exceptionDetails"].get("exception", {}).get("description")}')
		self._context_id = world['executionContextId']
		self.rules = checked
		return len(checked)

	async def wait(self, timeout: float = 30.0) -> ReflexEvent | None:
		"""The next event a notify rule raised, or None after `timeout` seconds."""
		try:
			return await asyncio.wait_for(self.events.get(), timeout)
		except TimeoutError:
			return None

	def stats(self) -> dict[str, Any]:
		lat = sorted(self._latencies)
		out: dict[str, Any] = {
			'rules': len(self.rules),
			'actions': self._actions,
			'latency_ms_p50': round(statistics.median(lat), 1) if lat else None,
			'latency_ms_p95': round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1) if lat else None,
			'held': sorted(self._held),
			'errors': self.errors[-5:],
		}
		if self._screen_fed:
			# the rules read the screen: how many pictures, how old one is when it reaches them, and where they come
			# from (a screencast, or captures: slower, ~50 ms or more each)
			out['screen_pictures'] = self._screen_fed
			out['screen_ms_p50'] = round(statistics.median(self._screen_ms), 1)
			out['screen_source'] = self._screen_source
		return out

	async def clear(self) -> None:
		"""Stop the rules and let go of any key they hold."""
		if self._screen_task is not None:
			self._screen_task.cancel()
			await asyncio.gather(self._screen_task, return_exceptions=True)
			self._screen_task = None
		if self._cdp is not None and self._session_id:
			try:
				tree = await self._cdp.cdp_client.send.Page.getFrameTree(session_id=self._session_id)
				world = await self._cdp.cdp_client.send.Page.createIsolatedWorld(
					params={'frameId': tree['frameTree']['frame']['id'], 'worldName': WORLD_NAME}, session_id=self._session_id
				)
				await self._cdp.cdp_client.send.Runtime.evaluate(
					params={'expression': 'window.__reflex && window.__reflex.stop()', 'contextId': world['executionContextId']},
					session_id=self._session_id,
				)
			except Exception as e:
				logger.debug(f'reflex: stop failed: {type(e).__name__}: {e}')
			# what the rules decided before they stopped goes out (or fails) before anything else happens on this tab
			if self._tasks:
				await asyncio.gather(*list(self._tasks), return_exceptions=True)
			for key in list(self._held):
				await self._key(key, 'keyUp')
		self._held.clear()
		registry = getattr(self.session.cdp_client, '_event_registry', None)
		handlers = getattr(registry, '_handlers', None)
		if self._handler is not None and isinstance(handlers, dict) and handlers.get(_BINDING_EVENT) is self._handler:
			if self._incumbent is not None:
				self.session.cdp_client.register.Runtime.bindingCalled(self._incumbent)
			else:
				handlers.pop(_BINDING_EVENT, None)
		self._handler = None
		self.rules = []

	# -- the loop's fast path ----------------------------------------------------------------------------------------

	def _on_binding(self, event: dict[str, Any], session_id: str | None = None) -> None:
		if event.get('name') != BINDING:
			if self._incumbent is not None:
				return self._incumbent(event, session_id)
			return None
		try:
			msg = json.loads(event.get('payload') or '{}')
		except ValueError:
			return None
		action = msg.get('a')
		if action == 'lat':
			self._latencies.append(float(msg.get('ms') or 0))
			del self._latencies[:-2000]
		elif action == 'notify':
			# told only once what the rules did before it has gone out: a click and its notify fire in one frame
			task = asyncio.create_task(self._notify_after(list(self._tasks), msg))
			self._tasks.add(task)
			task.add_done_callback(self._tasks.discard)
		elif action == 'error':
			self.errors.append(f'rule {msg.get("r")}: {msg.get("error")}')
		elif action == 'screen' and self._screen_task is None:
			self._screen_task = asyncio.create_task(self._feed_screen())
		elif action in ('down', 'up', 'tap', 'click'):
			task = asyncio.create_task(self._act(msg))
			self._tasks.add(task)
			task.add_done_callback(self._tasks.discard)
		return None

	async def _feed_screen(self) -> None:
		"""Keep the rules fed with the screen as the compositor draws it, for what the page cannot read itself.

		From a screencast on a connection of its own when this session launched the browser: frames arrive as the
		compositor makes them, ~1 ms old, at the page's own rate. Otherwise, or if that connection cannot be opened,
		from captures: each forces a frame and takes ~50 ms, more on a busy page (~150 ms on a 1080p film, measured
		live). Either way the feed ends with its browser, not with the process.
		"""
		assert self._cdp is not None and self._context_id is not None
		try:
			if self.session.is_local:
				try:
					await self._feed_by_screencast()
					return
				except _NoScreencast as e:
					logger.debug(f'reflex: no screencast for the screen ({e}); capturing instead')
			self._screen_source = 'captures'
			await self._feed_by_capture()
		except asyncio.CancelledError:
			raise
		except Exception as e:
			self._screen_error(e)
		finally:
			self._screen_task = None if self._screen_task is asyncio.current_task() else self._screen_task

	async def _feed_by_capture(self) -> None:
		failing_since: float | None = None
		while self._handler is not None:
			started = time.time()
			try:
				png, span = await self._capture()
			except asyncio.CancelledError:
				raise
			except Exception as e:
				self._screen_error(e)
				failing_since = failing_since or started
				if started - failing_since > SCREEN_GIVE_UP_S:
					return  # no capture for that long: the tab or its browser is gone
				await asyncio.sleep(0.25)  # a navigation: try again shortly
				continue
			failing_since = None
			await self._feed(png, started, span)

	async def _feed_by_screencast(self) -> None:
		"""A screencast of this tab on a DevTools connection of its own, so it shares no event slot with the recorder
		or the eyes. A still page sends nothing; and the repaint that ends an animation can be dropped while a frame
		is in flight (as the recorder found), so a stream gone quiet is topped up with one capture.

		It once also sent "broken frames" (a playing video missing, or the whole frame black), each checked with a
		capture before it was fed. Those were this feed's own clipped captures showing in the screencast; captures no
		longer clip (see browser_use.browser.capture), and without the check the screen tests passed 20 runs of 20,
		where they had failed 3-4 in 10. So a real cut is fed as it comes, without a capture's ~40 ms."""
		from urllib.parse import urlparse

		import websockets

		assert self._cdp is not None
		where = urlparse(self.session.cdp_url or '')
		assert where.hostname and where.port, f'no local DevTools address in {self.session.cdp_url!r}'
		url = f'ws://{where.hostname}:{where.port}/devtools/page/{self._cdp.target_id}'
		try:
			connection = await websockets.connect(url, max_size=None, proxy=None, open_timeout=5)
		except Exception as e:
			raise _NoScreencast(f'{type(e).__name__}: {e}') from e
		self._screen_source = 'screencast'
		async with connection as ws:
			newest: list[tuple[bytes, float, tuple[float, float] | None] | None] = [None]
			arrived = asyncio.Event()

			async def read() -> None:
				async for raw in ws:
					msg = json.loads(raw)
					if msg.get('method') != 'Page.screencastFrame':
						continue
					params = msg['params']
					ack = {'id': 2, 'method': 'Page.screencastFrameAck', 'params': {'sessionId': params['sessionId']}}
					await ws.send(json.dumps(ack))  # at once: Chrome sends the next frame only after this
					meta = params.get('metadata', {})
					drawn = float(meta.get('timestamp') or time.time())
					span = (float(meta['deviceWidth']), float(meta['deviceHeight'])) if meta.get('deviceWidth') else None
					newest[0] = (base64.b64decode(params['data']), drawn, span)  # an older frame not yet fed is dropped
					arrived.set()

			reader = asyncio.create_task(read())
			try:
				start = {'format': 'png', 'maxWidth': SCREEN_WIDTH, 'maxHeight': SCREEN_WIDTH * 4, 'everyNthFrame': 1}
				await ws.send(json.dumps({'id': 1, 'method': 'Page.startScreencast', 'params': start}))
				shown, span = await self._capture()  # a still page sends no frame until it changes
				await self._feed(shown, time.time(), span)
				topped_up = True
				while self._handler is not None:
					if reader.done():
						reader.result()
						return  # the connection closed: the tab or its browser is gone
					try:
						await asyncio.wait_for(arrived.wait(), SCREEN_QUIET_S)
					except TimeoutError:
						if not topped_up:
							topped_up = True
							shown, span = await self._capture()
							await self._feed(shown, time.time(), span)
						continue
					arrived.clear()
					frame, newest[0] = newest[0], None
					if frame is not None:
						topped_up = False
						await self._feed(*frame)
			finally:
				reader.cancel()
				await asyncio.gather(reader, return_exceptions=True)

	async def _capture(self) -> tuple[bytes, tuple[float, float]]:
		"""The viewport now, as a PNG SCREEN_WIDTH wide (forces a frame: ~50 ms headless, more on a busy page), and the
		CSS px it spans: the page's client area, without its scrollbar."""
		assert self._cdp is not None
		send = self._cdp.cdp_client.send
		vp = (await send.Page.getLayoutMetrics(session_id=self._session_id))['cssLayoutViewport']
		clip = {
			'x': vp['pageX'],
			'y': vp['pageY'],
			'width': vp['clientWidth'],
			'height': vp['clientHeight'],
			'scale': SCREEN_WIDTH / max(1, vp['clientWidth']),
		}
		shot = await capture_screenshot(self._cdp.cdp_client, {'format': 'png', 'clip': clip}, self._session_id)
		return base64.b64decode(shot['data']), (float(vp['clientWidth']), float(vp['clientHeight']))

	async def _feed(self, png: bytes, drawn: float, span: tuple[float, float] | None = None) -> None:
		"""Hand one picture to the rules; `drawn` is when the compositor drew it (time.time()), `span` the CSS px it
		covers. A screencast frame takes in the window, its scrollbar too; a capture only the client area: mapped
		alike, one was ~1% out (found aiming a rule at Temple Run 2's on-screen key on Poki)."""
		assert self._cdp is not None and self._context_id is not None
		try:
			w, h, rgb = await asyncio.to_thread(_rgb, png)
			self._screen_seq += 1
			await self._cdp.cdp_client.send.Runtime.evaluate(
				params={
					'expression': f'window.__reflex && window.__reflex.feed({w}, {h}, "{base64.b64encode(rgb).decode()}", {self._screen_seq}, {drawn!r}, {json.dumps(list(span) if span else None)})',
					'contextId': self._context_id,
				},
				session_id=self._session_id,
			)
		except asyncio.CancelledError:
			raise
		except Exception as e:
			self._screen_error(e)
			return
		self._screen_fed += 1
		self._screen_ms.append((time.time() - drawn) * 1000)
		del self._screen_ms[:-500]

	def _screen_error(self, e: Exception) -> None:
		if self._handler is not None:
			self.errors.append(f'screen: {type(e).__name__}: {e}')
			del self.errors[:-50]

	async def _notify_after(self, before: list[asyncio.Task], msg: dict[str, Any]) -> None:
		if before:
			await asyncio.gather(*before, return_exceptions=True)
		self.events.put_nowait(
			ReflexEvent(
				label=str(msg.get('label')),
				rule=int(msg.get('r', -1)),
				page_ms=float(msg.get('wt') or 0),
				media_t=float(msg['mt']) if isinstance(msg.get('mt'), int | float) else None,
				wall=time.monotonic(),
			)
		)

	async def _act(self, msg: dict[str, Any]) -> None:
		action = msg['a']
		try:
			if action == 'click':
				await self._click(float(msg['x']), float(msg['y']))
			elif action == 'down':
				await self._key(msg['key'], 'keyDown')
				self._held.add(msg['key'])
			elif action == 'up':
				await self._key(msg['key'], 'keyUp')
				self._held.discard(msg['key'])
			else:
				await self._key(msg['key'], 'keyDown')
				await self._key(msg['key'], 'keyUp')
			self._actions += 1
		except Exception as e:
			self.errors.append(f'{action}: {type(e).__name__}: {e}')

	async def _key(self, key: str, kind: str) -> None:
		assert self._cdp is not None
		await self._cdp.cdp_client.send.Input.dispatchKeyEvent(params=_key_params(key, kind), session_id=self._session_id)

	async def _click(self, x: float, y: float) -> None:
		assert self._cdp is not None
		send = self._cdp.cdp_client.send.Input.dispatchMouseEvent
		for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
			params: dict[str, Any] = {'type': kind, 'x': x, 'y': y}
			if kind != 'mouseMoved':
				params |= {'button': 'left', 'clickCount': 1}
			await send(params=params, session_id=self._session_id)
