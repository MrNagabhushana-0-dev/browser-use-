"""Touch: swipes, flicks and taps, dispatched as real touch events over CDP.

Short-form video feeds are built for a thumb. A flick up on a Reels or Shorts feed is the
native "next", and the feed's own gesture code decides from the stroke — its distance,
duration, and above all its speed at the moment the finger lifts — whether it was a flick
to the next item or a nudge that springs back.

That last detail is why the profile here is not a plain minimum-jerk curve. Minimum jerk
(position s(t) = 10t^3 - 15t^4 + 6t^5, Flash & Hogan 1985) is the classic model of a
point-to-point reach, and it arrives at zero velocity. A flick does not: the thumb leaves the
glass while still moving fast, and that release velocity is what carries momentum. So a
flick follows the first `release` fraction of a longer minimum-jerk reach, rescaled to the
requested distance, and lifts off mid-stroke. The stroke also bows slightly sideways, as a
thumb pivoting on its joint does, and each sample carries a contact radius and force.

Events are `Input.dispatchTouchEvent`, so the page receives trusted `touchstart` /
`touchmove` / `touchend` (and the pointer events derived from them) at the real hit-test
target. Chrome accepts these without touch emulation; `enable_touch_emulation()` is there
for sites that check `navigator.maxTouchPoints` before wiring up their gestures.
"""

import asyncio
import logging
import math
import random
import time
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

Direction = Literal['up', 'down', 'left', 'right']

# Sample rate of a touch digitizer, roughly. More samples add CDP round trips, not realism.
SAMPLE_HZ = 60.0
# Share of a minimum-jerk reach completed before the thumb lifts. At 0.75 the release speed
# is ~45% of peak: a flick, not a drag.
FLICK_RELEASE = 0.75


def min_jerk(t: float) -> float:
	return 10 * t**3 - 15 * t**4 + 6 * t**5


def trajectory(
	start: tuple[float, float],
	end: tuple[float, float],
	rng: random.Random,
	release: float = 1.0,
	bow: float = 0.03,
):
	"""Position of a thumb stroke from start to end as a function of u in [0, 1].

	With release < 1 the stroke is the first `release` of a longer minimum-jerk reach, scaled
	so that it still ends at `end`: it ends while moving.
	"""
	assert 0 < release <= 1, 'release must be in (0, 1]'
	(x0, y0), (x1, y1) = start, end
	dx, dy = x1 - x0, y1 - y0
	length = math.hypot(dx, dy) or 1.0
	nx, ny = -dy / length, dx / length  # unit normal, for the sideways bow
	amp = bow * length * rng.uniform(0.5, 1.0) * rng.choice((-1, 1))
	scale = min_jerk(release)

	def pos(u: float) -> tuple[float, float]:
		s = min_jerk(max(0.0, min(1.0, u)) * release) / scale
		side = amp * math.sin(math.pi * s) + rng.gauss(0, 0.4)
		return (x0 + dx * s + nx * side, y0 + dy * s + ny * side)

	return pos


def stroke(
	start: tuple[float, float],
	end: tuple[float, float],
	duration_ms: float,
	rng: random.Random,
	release: float = 1.0,
	bow: float = 0.03,
) -> list[tuple[float, float, float]]:
	"""(x, y, ms since start) samples of the stroke at SAMPLE_HZ, for inspection and tests."""
	assert duration_ms > 0, 'duration must be positive'
	pos = trajectory(start, end, rng, release=release, bow=bow)
	n = max(4, int(duration_ms / 1000 * SAMPLE_HZ))
	return [(*pos(i / n), i / n * duration_ms) for i in range(n + 1)]


def release_speed(points: list[tuple[float, float, float]]) -> float:
	"""Speed over the last two samples, in px/ms."""
	(xa, ya, ta), (xb, yb, tb) = points[-2], points[-1]
	return math.hypot(xb - xa, yb - ya) / max(1e-6, tb - ta)


class HumanTouch:
	"""A thumb on the page. One instance per session; seedable for tests."""

	def __init__(self, browser_session: 'BrowserSession', seed: int | None = None) -> None:
		self.browser_session = browser_session
		self.rng = random.Random(seed)
		self._touch_id = 0

	async def _session(self, target_id=None):
		return await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

	async def viewport(self, target_id=None) -> tuple[float, float]:
		cdp = await self._session(target_id)
		metrics = await cdp.cdp_client.send.Page.getLayoutMetrics(session_id=cdp.session_id)
		vp = metrics.get('cssVisualViewport') or metrics.get('cssLayoutViewport') or {}
		return float(vp.get('clientWidth') or 1280), float(vp.get('clientHeight') or 800)

	async def enable_touch_emulation(self, target_id=None, max_points: int = 5) -> None:
		cdp = await self._session(target_id)
		await cdp.cdp_client.send.Emulation.setTouchEmulationEnabled(
			params={'enabled': True, 'maxTouchPoints': max_points}, session_id=cdp.session_id
		)

	def _point(self, x: float, y: float) -> dict[str, Any]:
		# A fingertip is an ellipse ~10-14 CSS px across, pressed lightly.
		return {
			'x': x,
			'y': y,
			'radiusX': self.rng.uniform(5, 7),
			'radiusY': self.rng.uniform(6, 8),
			'force': self.rng.uniform(0.35, 0.65),
			'id': self._touch_id,
		}

	async def _send(self, cdp, kind: str, points: list[dict[str, Any]]) -> None:
		await cdp.cdp_client.send.Input.dispatchTouchEvent(
			params={'type': kind, 'touchPoints': points}, session_id=cdp.session_id
		)

	async def swipe(
		self,
		start: tuple[float, float],
		end: tuple[float, float],
		duration_ms: float | None = None,
		release: float = 1.0,
		target_id=None,
	) -> dict[str, float]:
		"""Drag a finger from start to end. With release < 1 it lifts while still moving."""
		distance = math.hypot(end[0] - start[0], end[1] - start[1])
		if duration_ms is None:
			duration_ms = min(600.0, max(90.0, 110 + distance * 0.25)) * self.rng.uniform(0.85, 1.2)
		points = stroke(start, end, duration_ms, random.Random(0), release=release)  # for the report only
		cdp = await self._session(target_id)
		self._touch_id += 1
		# Positions are taken at the time each event is actually sent, not at a planned time. A
		# busy event loop that oversleeps would otherwise send two stale samples back to back,
		# which the page reads as a violent flick (seen under CPU load: the feed flew past the
		# next item). Stamping events with planned timestamps instead made it worse: Chrome
		# consistently over-flung, so the timeline here is the real one.
		pos = trajectory(start, end, self.rng, release=release)
		started = time.monotonic()
		await self._send(cdp, 'touchStart', [self._point(*pos(0.0))])
		try:
			u = 0.0
			while u < 1.0:
				await asyncio.sleep(1 / SAMPLE_HZ)
				u = min(1.0, (time.monotonic() - started) * 1000 / duration_ms)
				await self._send(cdp, 'touchMove', [self._point(*pos(u))])
		finally:
			try:
				await self._send(cdp, 'touchEnd', [])
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.debug(f'touchEnd failed while unwinding: {type(e).__name__}: {e}')
		return {'distance_px': distance, 'duration_ms': duration_ms, 'release_px_per_ms': release_speed(points)}

	async def flick(
		self,
		direction: Direction = 'up',
		fraction: float = 0.55,
		around: tuple[float, float] | None = None,
		target_id=None,
	) -> dict[str, float]:
		"""A quick thumb flick across `fraction` of the viewport. 'up' moves content up (next item)."""
		assert 0.05 <= fraction <= 0.9, 'fraction of the viewport must be within 0.05-0.9'
		w, h = await self.viewport(target_id)
		cx, cy = around or (w * self.rng.uniform(0.45, 0.6), h * self.rng.uniform(0.45, 0.55))
		span = (h if direction in ('up', 'down') else w) * fraction
		sign = {'up': -1, 'down': 1, 'left': -1, 'right': 1}[direction]
		if direction in ('up', 'down'):
			start = (cx + self.rng.uniform(-8, 8), cy - sign * span / 2)
			end = (cx + self.rng.uniform(-20, 20), cy + sign * span / 2)
		else:
			start = (cx - sign * span / 2, cy + self.rng.uniform(-8, 8))
			end = (cx + sign * span / 2, cy + self.rng.uniform(-20, 20))
		start = (min(max(start[0], 2), w - 2), min(max(start[1], 2), h - 2))
		end = (min(max(end[0], 2), w - 2), min(max(end[1], 2), h - 2))
		duration = self.rng.uniform(140, 230)
		return await self.swipe(start, end, duration_ms=duration, release=FLICK_RELEASE, target_id=target_id)

	async def tap(self, x: float, y: float, target_id=None) -> None:
		"""A light tap: down, 50-110 ms of contact with a pixel of drift, up."""
		cdp = await self._session(target_id)
		self._touch_id += 1
		x += self.rng.gauss(0, 1.5)
		y += self.rng.gauss(0, 1.5)
		await self._send(cdp, 'touchStart', [self._point(x, y)])
		try:
			await asyncio.sleep(self.rng.uniform(0.05, 0.11))
			await self._send(cdp, 'touchMove', [self._point(x + self.rng.uniform(-0.8, 0.8), y + self.rng.uniform(-0.8, 0.8))])
		finally:
			try:
				await self._send(cdp, 'touchEnd', [])
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.debug(f'touchEnd failed while unwinding: {type(e).__name__}: {e}')

	async def long_press(self, x: float, y: float, seconds: float = 0.6, target_id=None) -> None:
		cdp = await self._session(target_id)
		self._touch_id += 1
		await self._send(cdp, 'touchStart', [self._point(x, y)])
		try:
			await asyncio.sleep(seconds)
		finally:
			try:
				await self._send(cdp, 'touchEnd', [])
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.debug(f'touchEnd failed while unwinding: {type(e).__name__}: {e}')
