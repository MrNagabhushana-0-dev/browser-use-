"""Drive the browser through real input events instead of scripting the DOM.

`element.click()` and `dispatchEvent` produce events with `isTrusted === false`, skip
every intermediate `mousemove`/`mouseover`, and bypass the browser's own hit testing — so
they land on the element you named even when something is covering it. Events dispatched
through CDP's Input domain are the opposite on all three counts: `isTrusted === true`,
routed through the real event pipeline, and delivered to whatever is actually under the
cursor.

That difference decides whether a hover menu opens, whether a drag starts, whether an
infinite feed advances, and whether a site that scores automation lets you through.

This module is the mechanical layer. It takes coordinates and text, and produces the
event stream a hand would have produced.
"""

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any, Literal

from browser_use.human.motion import (
	bezier_path,
	click_dwell_ms,
	keystroke_delays,
	landing_point,
	move_duration_ms,
)

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

MouseButton = Literal['left', 'right', 'middle']

# A wheel notch in Chrome. Real wheels move in notches, not smooth pixel deltas, and some
# feeds (Shorts, Reels) count notches to decide whether you flicked or nudged.
WHEEL_NOTCH_PX = 100.0


class HumanInput:
	"""Synthesizes the input a person would produce, over CDP.

	One instance per session. Pointer position is retained between calls, because a hand
	does not teleport back to the origin between two clicks — and because a page watching
	`mousemove` sees a continuous track rather than a series of apparitions.
	"""

	def __init__(self, browser_session: 'BrowserSession', seed: int | None = None) -> None:
		self.browser_session = browser_session
		# Seedable so tests are deterministic; unseeded in real use so two runs never
		# produce byte-identical timing.
		self.rng = random.Random(seed)
		self.x: float = 0.0
		self.y: float = 0.0

	@property
	def logger(self):
		return self.browser_session.logger

	async def _session(self, target_id=None):
		return await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

	async def _mouse(self, cdp, event_type: str, x: float, y: float, **extra: Any) -> None:
		params: dict[str, Any] = {'type': event_type, 'x': x, 'y': y, **extra}
		await cdp.cdp_client.send.Input.dispatchMouseEvent(params=params, session_id=cdp.session_id)

	# -- pointer ---------------------------------------------------------------------

	async def move_to(self, x: float, y: float, target_id=None) -> None:
		"""Glide the pointer to (x, y), emitting the moves along the way."""
		cdp = await self._session(target_id)
		path = bezier_path((self.x, self.y), (x, y), self.rng)
		total_ms = move_duration_ms(((x - self.x) ** 2 + (y - self.y) ** 2) ** 0.5, self.rng)
		per_step = (total_ms / max(1, len(path))) / 1000.0

		for px, py in path:
			await self._mouse(cdp, 'mouseMoved', px, py)
			# The sleep is the point: without it every move lands in the same millisecond
			# and the page sees a teleport with extra steps.
			await asyncio.sleep(per_step)
		self.x, self.y = x, y

	async def click(
		self,
		x: float,
		y: float,
		button: MouseButton = 'left',
		click_count: int = 1,
		target_id=None,
	) -> None:
		"""Move to the point, settle, press, hold briefly, release."""
		await self.move_to(x, y, target_id=target_id)
		cdp = await self._session(target_id)

		# A hand pauses on arrival before committing. Hover handlers need this too.
		await asyncio.sleep(self.rng.uniform(0.03, 0.12))

		await self._mouse(cdp, 'mousePressed', self.x, self.y, button=button, clickCount=click_count)
		await asyncio.sleep(click_dwell_ms(self.rng) / 1000.0)
		await self._mouse(cdp, 'mouseReleased', self.x, self.y, button=button, clickCount=click_count)

	async def click_box(self, box: tuple[float, float, float, float], target_id=None, **kwargs) -> None:
		"""Click somewhere sensible inside (x, y, width, height), not dead centre."""
		bx, by, bw, bh = box
		cx, cy = landing_point(bx + bw / 2, by + bh / 2, bw, bh, self.rng)
		await self.click(cx, cy, target_id=target_id, **kwargs)

	async def wheel(self, delta_y: float, delta_x: float = 0.0, target_id=None) -> None:
		"""Scroll with real wheel events, delivered in notches.

		This is the difference that makes custom scroll containers work. `window.scrollBy`
		moves the document and fires `scroll`; it never fires `wheel`, so anything that
		implements its own scrolling on top of `wheel` — short-form video feeds, virtualized
		lists, map canvases — simply does not move.
		"""
		cdp = await self._session(target_id)
		notches = max(1, int(abs(delta_y) / WHEEL_NOTCH_PX) or 1)
		step_y = delta_y / notches
		step_x = delta_x / notches
		for _ in range(notches):
			await self._mouse(
				cdp,
				'mouseWheel',
				self.x,
				self.y,
				deltaX=step_x * self.rng.uniform(0.9, 1.1),
				deltaY=step_y * self.rng.uniform(0.9, 1.1),
			)
			await asyncio.sleep(self.rng.uniform(0.02, 0.06))

	# -- keyboard --------------------------------------------------------------------

	async def type_text(self, text: str, wpm: float = 260.0, target_id=None) -> None:
		"""Type character by character with human cadence."""
		cdp = await self._session(target_id)
		for char, delay_ms in zip(text, keystroke_delays(text, self.rng, wpm=wpm)):
			await cdp.cdp_client.send.Input.dispatchKeyEvent(
				params={'type': 'keyDown', 'text': char, 'key': char, 'unmodifiedText': char},
				session_id=cdp.session_id,
			)
			await asyncio.sleep(max(0.004, delay_ms / 2000.0))
			await cdp.cdp_client.send.Input.dispatchKeyEvent(
				params={'type': 'keyUp', 'key': char},
				session_id=cdp.session_id,
			)
			await asyncio.sleep(max(0.004, delay_ms / 2000.0))

	async def press(self, key: str, code: str | None = None, key_code: int | None = None, target_id=None) -> None:
		"""Press and release a named key such as Enter, Tab, ArrowDown or Escape."""
		cdp = await self._session(target_id)
		params: dict[str, Any] = {'key': key}
		if code:
			params['code'] = code
		if key_code is not None:
			params['windowsVirtualKeyCode'] = key_code
			params['nativeVirtualKeyCode'] = key_code

		await cdp.cdp_client.send.Input.dispatchKeyEvent(
			params={'type': 'keyDown', **params},  # type: ignore[arg-type]
			session_id=cdp.session_id,
		)
		await asyncio.sleep(self.rng.uniform(0.04, 0.11))
		await cdp.cdp_client.send.Input.dispatchKeyEvent(
			params={'type': 'keyUp', **params},  # type: ignore[arg-type]
			session_id=cdp.session_id,
		)
