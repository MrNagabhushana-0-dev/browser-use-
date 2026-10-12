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
import json
import logging
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

WORLD_NAME = 'browser_use_reflex'
BINDING = '__browserUseReflex'
_BINDING_EVENT = 'Runtime.bindingCalled'
_SCRIPT = (Path(__file__).parent / 'reflex.js').read_text()

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
	wall: float  # time.monotonic() when it reached Python


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

	@property
	def installed(self) -> bool:
		return self._handler is not None

	async def install(self, rules: list[dict[str, Any] | ReflexRule]) -> int:
		"""Run these rules on the focused tab from the next frame on, replacing any installed before."""
		checked = [r if isinstance(r, ReflexRule) else ReflexRule.model_validate(r) for r in rules]
		assert 1 <= len(checked) <= 50, 'between 1 and 50 rules'
		await self.clear()
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
		return {
			'rules': len(self.rules),
			'actions': self._actions,
			'latency_ms_p50': round(statistics.median(lat), 1) if lat else None,
			'latency_ms_p95': round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1) if lat else None,
			'held': sorted(self._held),
			'errors': self.errors[-5:],
		}

	async def clear(self) -> None:
		"""Stop the rules and let go of any key they hold."""
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
		elif action in ('down', 'up', 'tap', 'click'):
			task = asyncio.create_task(self._act(msg))
			self._tasks.add(task)
			task.add_done_callback(self._tasks.discard)
		return None

	async def _notify_after(self, before: list[asyncio.Task], msg: dict[str, Any]) -> None:
		if before:
			await asyncio.gather(*before, return_exceptions=True)
		self.events.put_nowait(
			ReflexEvent(
				label=str(msg.get('label')), rule=int(msg.get('r', -1)), page_ms=float(msg.get('wt') or 0), wall=time.monotonic()
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
